"""IBKR Flex Poller — polls Trade Confirmation Flex Queries and fires webhooks."""

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("poller")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
FLEX_TOKEN = os.environ.get("IBKR_FLEX_TOKEN", "")
FLEX_QUERY_ID = os.environ.get("IBKR_FLEX_QUERY_ID", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "600"))
TARGET_WEBHOOK_URL = os.environ.get("TARGET_WEBHOOK_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
DB_PATH = os.environ.get("DB_PATH", "/data/poller.db")

FLEX_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
USER_AGENT = "ibkr-relay/1.0"


# ---------------------------------------------------------------------------
# SQLite — deduplication of processed fills
# ---------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_fills (
            exec_id TEXT PRIMARY KEY,
            processed_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def get_processed_ids(conn, exec_ids):
    """Return the subset of exec_ids already in the DB."""
    if not exec_ids:
        return set()
    placeholders = ",".join("?" for _ in exec_ids)
    rows = conn.execute(
        f"SELECT exec_id FROM processed_fills WHERE exec_id IN ({placeholders})",
        list(exec_ids),
    ).fetchall()
    return {r[0] for r in rows}


def mark_processed(conn, exec_ids):
    conn.executemany(
        "INSERT OR IGNORE INTO processed_fills (exec_id) VALUES (?)",
        [(eid,) for eid in exec_ids],
    )
    conn.commit()


def prune_old(conn, days=30):
    conn.execute(
        "DELETE FROM processed_fills WHERE processed_at < datetime('now', ?)",
        (f"-{days} days",),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Webhook delivery
# ---------------------------------------------------------------------------
def send_webhook(payload: dict) -> None:
    body = json.dumps(payload, default=str, indent=2)

    if not TARGET_WEBHOOK_URL:
        log.info("Webhook payload (dry-run):\n%s", body)
        return

    signature = hmac.new(
        WEBHOOK_SECRET.encode(), body.encode(), hashlib.sha256
    ).hexdigest()

    try:
        resp = httpx.post(
            TARGET_WEBHOOK_URL,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Signature-256": f"sha256={signature}",
            },
            timeout=10.0,
        )
        log.info("Webhook sent — status %d", resp.status_code)
    except httpx.HTTPError as exc:
        log.error("Webhook delivery failed: %s", exc)


API_TOKEN = os.environ.get("API_TOKEN", "")
API_PORT = int(os.environ.get("POLLER_API_PORT", "8000"))


# ---------------------------------------------------------------------------
# Flex Web Service
# ---------------------------------------------------------------------------
def fetch_flex_report(flex_token=None, flex_query_id=None):
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

        return resp.text

    log.error("Report generation timed out after retries")
    return None


def parse_trades(xml_text):
    """Parse Flex Query XML for trade records."""
    root = ET.fromstring(xml_text)
    trades = []
    seen = set()

    # Trade Confirmation queries use <TradeConfirmation>,
    # Activity queries use <Trade> and <Order>. We only want <Trade> entries
    # (individual fills), not <Order> (aggregated). Skip <Order> because it
    # has no transactionID and duplicates the fill data.
    for tag in ("TradeConfirmation", "Trade"):
        for el in root.iter(tag):
            exec_id = el.get("transactionID", "") or el.get("ibExecID", "")
            if not exec_id or exec_id in seen:
                continue
            seen.add(exec_id)

            try:
                qty = float(el.get("quantity", 0))
            except (ValueError, TypeError):
                qty = 0.0
            try:
                price = float(el.get("tradePrice", 0) or el.get("price", 0))
            except (ValueError, TypeError):
                price = 0.0
            try:
                commission = float(el.get("ibCommission", 0) or el.get("commission", 0))
            except (ValueError, TypeError):
                commission = 0.0

            trades.append({
                "event": "fill",
                "symbol": el.get("symbol", ""),
                "underlyingSymbol": el.get("underlyingSymbol", ""),
                "secType": el.get("assetCategory", ""),
                "exchange": el.get("listingExchange", "") or el.get("exchange", ""),
                "op": el.get("buySell", ""),
                "quantity": qty,
                "price": price,
                "tradeDate": el.get("tradeDate", ""),
                "tradeTime": el.get("dateTime", ""),
                "orderTime": el.get("orderTime", ""),
                "orderId": el.get("ibOrderID", "") or el.get("orderID", ""),
                "execId": exec_id,
                "account": el.get("accountId", ""),
                "commission": commission,
                "commissionCurrency": el.get("ibCommissionCurrency", ""),
                "currency": el.get("currency", ""),
                "orderType": el.get("orderType", ""),
            })

    return trades


def _ibkr_dt_to_iso(dt_str):
    """Convert IBKR datetime 'YYYYMMDD;HHmmss' to ISO 'YYYY-MM-DDTHH:MM:SS'."""
    try:
        d, t = dt_str.split(";")
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:{t[4:6]}"
    except (ValueError, IndexError):
        return dt_str


def _ibkr_date_to_iso(d):
    """Convert IBKR date 'YYYYMMDD' to ISO 'YYYY-MM-DD'."""
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return d


def aggregate_by_order(trades):
    """Group individual fills by orderId, compute weighted avg price."""
    groups = {}
    for t in trades:
        oid = t["orderId"]
        if not oid:
            continue
        groups.setdefault(oid, []).append(t)

    results = []
    for order_id, fills in groups.items():
        total_qty = sum(f["quantity"] for f in fills)
        abs_total = sum(abs(f["quantity"]) for f in fills)
        avg_price = (
            sum(abs(f["quantity"]) * f["price"] for f in fills) / abs_total
            if abs_total else 0.0
        )
        total_commission = sum(f["commission"] for f in fills)

        first = fills[0]
        results.append({
            "event": "fill",
            "symbol": first["symbol"],
            "underlyingSymbol": first["underlyingSymbol"],
            "secType": first["secType"],
            "exchange": first["exchange"],
            "op": first["op"],
            "quantity": total_qty,
            "avgPrice": round(avg_price, 8),
            "tradeDate": _ibkr_date_to_iso(max(f["tradeDate"] for f in fills)),
            "lastFillTime": _ibkr_dt_to_iso(max(f["tradeTime"] for f in fills)),
            "orderTime": _ibkr_dt_to_iso(first["orderTime"]),
            "orderId": order_id,
            "execIds": [f["execId"] for f in fills],
            "account": first["account"],
            "commission": round(total_commission, 4),
            "commissionCurrency": first["commissionCurrency"],
            "currency": first["currency"],
            "orderType": first["orderType"],
            "fillCount": len(fills),
        })

    return results


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------
def poll_once(conn=None, flex_token=None, flex_query_id=None):
    """Run a single poll. Returns list of new aggregated orders."""
    close_conn = conn is None
    if close_conn:
        conn = init_db()

    try:
        log.info("Polling Flex Web Service...")
        xml_text = fetch_flex_report(flex_token=flex_token, flex_query_id=flex_query_id)
        if xml_text is None:
            return []

        all_trades = parse_trades(xml_text)
        log.info("Parsed %d individual fill(s) from Flex report", len(all_trades))

        # Always show a sample of the first aggregated order for debugging
        all_orders = aggregate_by_order(all_trades)
        if all_orders:
            log.info("Sample order (first):\n%s", json.dumps(all_orders[0], default=str, indent=2))

        # Filter out already-processed fills
        all_exec_ids = {t["execId"] for t in all_trades}
        already_seen = get_processed_ids(conn, all_exec_ids)
        new_trades = [t for t in all_trades if t["execId"] not in already_seen]
        log.info("%d new fill(s) after dedup", len(new_trades))

        if not new_trades:
            log.info("No new fills")
            return []

        # Aggregate only the NEW fills by order
        orders = aggregate_by_order(new_trades)
        log.info("Aggregated into %d order(s)", len(orders))

        for order in orders:
            log.info(
                "New fill: %s %s %s @ avgPrice %s (qty %s, %d fill(s))",
                order["op"], order["symbol"], order["orderId"],
                order["avgPrice"], order["quantity"], order["fillCount"],
            )

        # Send a single webhook with all orders
        send_webhook({"trades": orders})

        # Mark all fills as processed after successful webhook
        all_new_exec_ids = [eid for o in orders for eid in o["execIds"]]
        mark_processed(conn, all_new_exec_ids)

        log.info("Sent 1 webhook with %d trade(s)", len(orders))
        return orders
    finally:
        if close_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# HTTP API — on-demand poll
# ---------------------------------------------------------------------------
_poll_lock = threading.Lock()
_db_conn = None


class PollHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def do_POST(self):
        if self.path != "/ibkr/run-poll":
            self._reply(404, {"error": "Not found"})
            return

        if not API_TOKEN:
            self._reply(500, {"error": "API_TOKEN not configured"})
            return
        auth = self.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {API_TOKEN}"):
            self._reply(401, {"error": "Unauthorized"})
            return

        # Read optional JSON body for token/query overrides
        flex_token = None
        flex_query_id = None
        content_len = int(self.headers.get("Content-Length", 0))
        if content_len > 0:
            try:
                body = json.loads(self.rfile.read(content_len))
                flex_token = body.get("ibkr_flex_token") or None
                flex_query_id = body.get("ibkr_flex_query_id") or None
            except (json.JSONDecodeError, Exception):
                pass  # ignore malformed body, fall back to env vars

        if not _poll_lock.acquire(blocking=False):
            self._reply(409, {"error": "Poll already in progress"})
            return
        try:
            orders = poll_once(_db_conn, flex_token=flex_token, flex_query_id=flex_query_id)
            result = orders if isinstance(orders, list) else []
            self._reply(200, {"trades": result})
        except Exception as exc:
            log.exception("On-demand poll failed")
            self._reply(500, {"error": str(exc)})
        finally:
            _poll_lock.release()

    def _reply(self, status, body):
        data = json.dumps(body, default=str, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_api_server():
    server = HTTPServer(("0.0.0.0", API_PORT), PollHandler)
    log.info("Poll API listening on 0.0.0.0:%d", API_PORT)
    server.serve_forever()


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def main_loop():
    """Continuous polling loop with HTTP API for on-demand polls."""
    global _db_conn
    if not FLEX_TOKEN or not FLEX_QUERY_ID:
        log.error("IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID must be set")
        raise SystemExit(1)

    log.info("IBKR Flex Poller starting (poll every %ds)", POLL_INTERVAL)
    if not TARGET_WEBHOOK_URL:
        log.info("No TARGET_WEBHOOK_URL — running in dry-run mode")

    _db_conn = init_db()
    prune_old(_db_conn)

    # Start HTTP API in background thread
    api_thread = threading.Thread(target=start_api_server, daemon=True)
    api_thread.start()

    while True:
        try:
            with _poll_lock:
                poll_once(_db_conn)
        except Exception:
            log.exception("Poll cycle failed")

        log.debug("Next poll in %ds", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


def main_once():
    """Single on-demand poll, then exit."""
    if not FLEX_TOKEN or not FLEX_QUERY_ID:
        log.error("IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID must be set")
        raise SystemExit(1)

    conn = init_db()
    orders = poll_once(conn)
    conn.close()
    n = len(orders) if isinstance(orders, list) else 0
    print(f"Done — {n} new trade(s) processed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        main_once()
    else:
        main_loop()
