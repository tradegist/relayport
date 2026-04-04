"""IBKR Flex Poller — core polling logic, SQLite dedup, and webhook delivery."""

import hashlib
import hmac
import logging
import os
import sqlite3
import time
import xml.etree.ElementTree as ET

import httpx

from models_poller import Trade, WebhookPayload
from .flex_parser import parse_fills, aggregate_fills, _dedup_id

log = logging.getLogger("poller")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
FLEX_TOKEN = os.environ.get("IBKR_FLEX_TOKEN", "")
FLEX_QUERY_ID = os.environ.get("IBKR_FLEX_QUERY_ID", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "600"))
TARGET_WEBHOOK_URL = os.environ.get("TARGET_WEBHOOK_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBHOOK_HEADER_NAME = os.environ.get("WEBHOOK_HEADER_NAME", "")
WEBHOOK_HEADER_VALUE = os.environ.get("WEBHOOK_HEADER_VALUE", "")
DB_PATH = os.environ.get("DB_PATH", "/data/poller.db")

FLEX_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
USER_AGENT = "ibkr-relay/1.0"


# ---------------------------------------------------------------------------
# SQLite — deduplication of processed fills
# ---------------------------------------------------------------------------
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_fills (
            exec_id TEXT PRIMARY KEY,
            processed_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn


def get_last_poll_ts(conn: sqlite3.Connection) -> str:
    """Return the last processed trade timestamp, or empty string."""
    row = conn.execute(
        "SELECT value FROM metadata WHERE key = 'last_poll_ts'"
    ).fetchone()
    return row[0] if row else ""


def set_last_poll_ts(conn: sqlite3.Connection, ts: str) -> None:
    """Update the last processed trade timestamp."""
    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_poll_ts', ?)",
        (ts,),
    )


def get_processed_ids(conn: sqlite3.Connection, exec_ids: set[str]) -> set[str]:
    """Return the subset of exec_ids already in the DB."""
    if not exec_ids:
        return set()
    placeholders = ",".join("?" for _ in exec_ids)
    rows = conn.execute(
        f"SELECT exec_id FROM processed_fills WHERE exec_id IN ({placeholders})",
        list(exec_ids),
    ).fetchall()
    return {r[0] for r in rows}


def mark_processed(conn: sqlite3.Connection, exec_ids: list[str]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO processed_fills (exec_id) VALUES (?)",
        [(eid,) for eid in exec_ids],
    )
    conn.commit()


def prune_old(conn: sqlite3.Connection, days: int = 30) -> None:
    conn.execute(
        "DELETE FROM processed_fills WHERE processed_at < datetime('now', ?)",
        (f"-{days} days",),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Webhook delivery
# ---------------------------------------------------------------------------
def send_webhook(payload: WebhookPayload) -> None:
    body = payload.model_dump_json(indent=2)

    if not TARGET_WEBHOOK_URL:
        log.info("Webhook payload (dry-run):\n%s", body)
        return

    signature = hmac.new(
        WEBHOOK_SECRET.encode(), body.encode(), hashlib.sha256
    ).hexdigest()

    try:
        headers = {
            "Content-Type": "application/json",
            "X-Signature-256": f"sha256={signature}",
        }
        if WEBHOOK_HEADER_NAME:
            headers[WEBHOOK_HEADER_NAME] = WEBHOOK_HEADER_VALUE

        resp = httpx.post(
            TARGET_WEBHOOK_URL,
            content=body,
            headers=headers,
            timeout=10.0,
        )
        log.info("Webhook sent — status %d", resp.status_code)
    except httpx.HTTPError as exc:
        log.error("Webhook delivery failed: %s", exc)


# ---------------------------------------------------------------------------
# Flex Web Service
# ---------------------------------------------------------------------------
def fetch_flex_report(flex_token: str | None = None, flex_query_id: str | None = None) -> str | None:
    """Two-step Flex Web Service: SendRequest -> GetStatement."""
    token = flex_token or FLEX_TOKEN
    query_id = flex_query_id or FLEX_QUERY_ID
    headers = {"User-Agent": USER_AGENT}

    # Step 1: request report generation
    resp = httpx.get(
        f"{FLEX_BASE}/SendRequest",
        params={"t": token, "q": query_id, "v": "3"},
        headers=headers,
        timeout=30.0,
    )
    resp.raise_for_status()

    root = ET.fromstring(resp.text)
    if root.findtext("Status") != "Success":
        code = root.findtext("ErrorCode", "?")
        msg = root.findtext("ErrorMessage", "Unknown error")
        log.error("SendRequest failed: [%s] %s", code, msg)
        return None

    ref_code = root.findtext("ReferenceCode")
    log.debug("SendRequest OK — ref=%s, waiting for report...", ref_code)

    # Step 2: poll for the generated report
    for wait in (5, 10, 15, 30):
        time.sleep(wait)
        resp = httpx.get(
            f"{FLEX_BASE}/GetStatement",
            params={"t": token, "q": ref_code, "v": "3"},
            headers=headers,
            timeout=60.0,
        )
        resp.raise_for_status()

        # Error responses are wrapped in <FlexStatementResponse>
        if resp.text.strip().startswith("<FlexStatementResponse"):
            err_root = ET.fromstring(resp.text)
            err_code = err_root.findtext("ErrorCode", "")
            if err_code == "1019":  # generation in progress
                log.debug("Report still generating, retrying...")
                continue
            msg = err_root.findtext("ErrorMessage", "Unknown error")
            log.error("GetStatement failed: [%s] %s", err_code, msg)
            return None

        return str(resp.text)

    log.error("Report generation timed out after retries")
    return None


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------
def poll_once(
    conn: sqlite3.Connection | None = None,
    flex_token: str | None = None,
    flex_query_id: str | None = None,
    debug: bool = False,
    replay: int = 0,
) -> list[Trade]:
    """Run a single poll. Returns list of new aggregated trades."""
    close_conn = conn is None
    if conn is None:
        conn = init_db()

    try:
        log.info("Polling Flex Web Service...")
        xml_text = fetch_flex_report(flex_token=flex_token, flex_query_id=flex_query_id)
        if xml_text is None:
            return []

        all_fills, parse_errors = parse_fills(xml_text)
        log.info("Parsed %d individual fill(s) from Flex report", len(all_fills))

        if parse_errors:
            for err in parse_errors:
                log.warning("Parse: %s", err)

        if debug:
            print("--- Raw Flex XML ---")
            print(xml_text)
            print("--- End Raw Flex XML ---")

        if all_fills:
            fill_times = [f.dateTime for f in all_fills]
            log.info("Trade time range: %s to %s", min(fill_times), max(fill_times))
            for f in all_fills:
                log.info("  Fill: %s %s dedup=%s dateTime=%s",
                         f.buySell, f.symbol, _dedup_id(f), f.dateTime)

        # Always show a sample of the first aggregated trade for debugging
        all_trades = aggregate_fills(all_fills)
        if all_trades:
            log.debug("Sample trade (first):\n%s", all_trades[0].model_dump_json(indent=2))

        # Pre-filter by timestamp watermark to reduce dedup work
        last_ts = get_last_poll_ts(conn)
        if last_ts:
            candidates = [f for f in all_fills if f.dateTime >= last_ts]
            log.info("Timestamp pre-filter: %d -> %d candidate(s) (watermark: %s)",
                     len(all_fills), len(candidates), last_ts)
            if len(candidates) < len(all_fills):
                filtered = [f for f in all_fills if f.dateTime < last_ts]
                for f in filtered:
                    log.info("  Filtered out: %s %s dateTime=%s < watermark %s",
                             f.buySell, f.symbol, f.dateTime, last_ts)
        else:
            candidates = all_fills
            log.info("No timestamp watermark — processing all %d fill(s)", len(candidates))

        # Dedup remaining candidates against stored exec IDs
        candidate_ids = {_dedup_id(f) for f in candidates}
        already_seen = get_processed_ids(conn, candidate_ids)
        new_fills = [f for f in candidates if _dedup_id(f) not in already_seen]
        log.info("%d new fill(s) after dedup", len(new_fills))

        if not new_fills:
            if replay and all_fills:
                replay_fills = all_fills[:replay]
                trades = aggregate_fills(replay_fills)
                log.info("Replay mode: resending %d fill(s) as %d trade(s)", len(replay_fills), len(trades))
                send_webhook(WebhookPayload(trades=trades, errors=parse_errors))
                return trades
            log.info("No new fills")
            return []

        # Aggregate only the NEW fills by order
        trades = aggregate_fills(new_fills)
        log.info("Aggregated into %d trade(s)", len(trades))

        for trade in trades:
            log.info(
                "New trade: %s %s %s @ price %s (qty %s, %d fill(s))",
                trade.buySell, trade.symbol, trade.orderId,
                trade.price, trade.quantity, trade.fillCount,
            )

        # Send a single webhook with all trades
        send_webhook(WebhookPayload(trades=trades, errors=parse_errors))

        # Mark all fills as processed after successful webhook
        all_new_ids = [did for t in trades for did in t.execIds]
        mark_processed(conn, all_new_ids)

        # Update timestamp watermark to the latest trade time
        max_ts = max(f.dateTime for f in new_fills)
        set_last_poll_ts(conn, max_ts)
        log.info("Updated timestamp watermark to %s", max_ts)

        log.info("Sent 1 webhook with %d trade(s)", len(trades))
        return trades
    finally:
        if close_conn:
            conn.close()
