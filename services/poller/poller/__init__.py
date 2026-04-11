"""IBKR Flex Poller — core polling logic and SQLite dedup."""

import logging
import os
import sqlite3
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from dedup import get_processed_ids, mark_processed_batch, prune
from dedup import init_db as _init_dedup_db
from notifier import notify
from notifier.base import BaseNotifier
from poller_models import Trade, WebhookPayloadTrades
from shared import DEDUP_DB_PATH, aggregate_fills

from .flex_parser import parse_fills

log = logging.getLogger("poller")

# ---------------------------------------------------------------------------
# Configuration — one getter per env var, single source of truth
# ---------------------------------------------------------------------------
FLEX_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
USER_AGENT = "ibkr-relay/1.0"


def get_flex_token() -> str:
    val = os.environ.get("IBKR_FLEX_TOKEN", "").strip()
    if not val:
        raise SystemExit("IBKR_FLEX_TOKEN must be set")
    return val


def get_flex_query_id() -> str:
    val = os.environ.get("IBKR_FLEX_QUERY_ID", "").strip()
    if not val:
        raise SystemExit("IBKR_FLEX_QUERY_ID must be set")
    return val


def get_poll_interval() -> int:
    raw = os.environ.get("POLL_INTERVAL_SECONDS", "600").strip()
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(
            f"Invalid POLL_INTERVAL_SECONDS={raw!r} — must be an integer"
        ) from None


def get_meta_db_path() -> str:
    val = os.environ.get("META_DB_PATH", "").strip()
    return val if val else "/data/meta/poller.db"


# ---------------------------------------------------------------------------
# SQLite — shared dedup DB + poller-specific metadata DB
# ---------------------------------------------------------------------------
def init_dedup_db(db_path: str | None = None) -> sqlite3.Connection:
    """Open the shared dedup database (cross-service, WAL mode)."""
    path = db_path or DEDUP_DB_PATH
    return _init_dedup_db(Path(path))


def init_meta_db(db_path: str | None = None) -> sqlite3.Connection:
    """Open the poller-specific metadata database (watermark)."""
    path = Path(db_path or get_meta_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn


def get_last_poll_ts(meta_conn: sqlite3.Connection) -> str:
    """Return the last processed trade timestamp, or empty string."""
    row = meta_conn.execute(
        "SELECT value FROM metadata WHERE key = 'last_poll_ts'"
    ).fetchone()
    return row[0] if row else ""


def set_last_poll_ts(meta_conn: sqlite3.Connection, ts: str) -> None:
    """Update the last processed trade timestamp."""
    meta_conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_poll_ts', ?)",
        (ts,),
    )
    meta_conn.commit()


def prune_old(dedup_conn: sqlite3.Connection, days: int = 30) -> None:
    prune(dedup_conn, days=days)


# ---------------------------------------------------------------------------
# Flex Web Service
# ---------------------------------------------------------------------------
def fetch_flex_report(flex_token: str | None = None, flex_query_id: str | None = None) -> str | None:
    """Two-step Flex Web Service: SendRequest -> GetStatement."""
    token = flex_token or get_flex_token()
    query_id = flex_query_id or get_flex_query_id()
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
    dedup_conn: sqlite3.Connection | None = None,
    meta_conn: sqlite3.Connection | None = None,
    flex_token: str | None = None,
    flex_query_id: str | None = None,
    debug: bool = False,
    replay: int = 0,
    notifiers: list[BaseNotifier] | None = None,
) -> list[Trade]:
    """Run a single poll. Returns list of new aggregated trades."""
    close_dedup = dedup_conn is None
    close_meta = meta_conn is None
    if dedup_conn is None:
        dedup_conn = init_dedup_db()
    if meta_conn is None:
        meta_conn = init_meta_db()

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
            fill_times = [f.timestamp for f in all_fills]
            log.info("Trade time range: %s to %s", min(fill_times), max(fill_times))
            for f in all_fills:
                log.info("  Fill: %s %s dedup=%s timestamp=%s",
                         f.side, f.symbol, f.execId, f.timestamp)

        # Always show a sample of the first aggregated trade for debugging
        all_trades = aggregate_fills(all_fills)
        if all_trades:
            log.debug("Sample trade (first):\n%s", all_trades[0].model_dump_json(indent=2))

        # Pre-filter by timestamp watermark to reduce dedup work
        last_ts = get_last_poll_ts(meta_conn)
        if last_ts:
            candidates = [f for f in all_fills if f.timestamp >= last_ts]
            log.info("Timestamp pre-filter: %d -> %d candidate(s) (watermark: %s)",
                     len(all_fills), len(candidates), last_ts)
            if len(candidates) < len(all_fills):
                filtered = [f for f in all_fills if f.timestamp < last_ts]
                for f in filtered:
                    log.info("  Filtered out: %s %s timestamp=%s < watermark %s",
                             f.side, f.symbol, f.timestamp, last_ts)
        else:
            candidates = all_fills
            log.info("No timestamp watermark — processing all %d fill(s)", len(candidates))

        # Dedup remaining candidates against stored exec IDs
        candidate_ids = {f.execId for f in candidates}
        already_seen = get_processed_ids(dedup_conn, candidate_ids)
        new_fills = [f for f in candidates if f.execId not in already_seen]
        log.info("%d new fill(s) after dedup", len(new_fills))

        if not new_fills:
            if replay and all_fills:
                sorted_fills = sorted(all_fills, key=lambda f: f.timestamp, reverse=True)
                replay_fills = sorted_fills[:replay]
                trades = aggregate_fills(replay_fills)
                log.info("Replay mode: resending %d fill(s) as %d trade(s)", len(replay_fills), len(trades))
                notify(notifiers or [], WebhookPayloadTrades(data=trades, errors=parse_errors))
                return trades
            log.info("No new fills")
            return []

        # Aggregate only the NEW fills by order
        trades = aggregate_fills(new_fills)
        log.info("Aggregated into %d trade(s)", len(trades))

        for trade in trades:
            log.info(
                "New trade: %s %s %s @ price %s (vol %s, %d fill(s))",
                trade.side, trade.symbol, trade.orderId,
                trade.price, trade.volume, trade.fillCount,
            )

        # Send a single webhook with all trades
        notify(notifiers or [], WebhookPayloadTrades(data=trades, errors=parse_errors))

        # Mark all fills as processed after successful webhook
        all_new_ids = [did for t in trades for did in t.execIds]
        mark_processed_batch(dedup_conn, all_new_ids)

        # Update timestamp watermark to the latest trade time
        max_ts = max(f.timestamp for f in new_fills)
        set_last_poll_ts(meta_conn, max_ts)
        log.info("Updated timestamp watermark to %s", max_ts)

        log.info("Sent 1 webhook with %d trade(s)", len(trades))
        return trades
    finally:
        if close_dedup:
            dedup_conn.close()
        if close_meta:
            meta_conn.close()
