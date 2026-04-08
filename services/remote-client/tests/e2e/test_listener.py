"""E2E tests — verify the listener fires webhooks on trade events.

The listener subscribes to ib_async execDetailsEvent and commissionReportEvent.
When the remote-client places a paper order, ib_async emits both events.
Each event fires a webhook to a receiver started by these tests.

NOTE: These tests only produce webhooks when the market is open and orders
actually fill.  When the market is closed, orders stay PreSubmitted and no
execution events are emitted.  Tests skip gracefully in that case.
"""

import hashlib
import hmac
import json
import threading
import time
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar

import httpx
import pytest

WEBHOOK_SECRET = "test-webhook-secret"


# ── Webhook receiver ────────────────────────────────────────────────────


class _WebhookHandler(BaseHTTPRequestHandler):
    """Collects incoming webhook payloads into a shared list."""

    received: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.received.append({
            "body": json.loads(body),
            "signature": self.headers.get("X-Signature-256", ""),
            "raw": body,
            "received_at": time.monotonic(),
        })
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass  # suppress access log noise during tests


@pytest.fixture(scope="module")
def webhook_payloads() -> Generator[list[dict[str, Any]]]:
    """Start a webhook receiver on port 19999 for the duration of this module."""
    _WebhookHandler.received = []
    server = HTTPServer(("0.0.0.0", 19999), _WebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield _WebhookHandler.received
    server.shutdown()


def _wait_for_fill(api: httpx.Client, order_id: int, timeout: float = 10) -> bool:
    """Return True if the order fills within timeout, False otherwise."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = api.get("/ibkr/trades")
        if resp.status_code == 200:
            for t in resp.json()["trades"]:
                if t["orderId"] == order_id and t["status"] == "Filled":
                    return True
        time.sleep(0.5)
    return False


# ── Tests ────────────────────────────────────────────────────────────────


def test_listener_fires_on_market_order(
    api: httpx.Client, webhook_payloads: list[dict[str, Any]],
) -> None:
    """Place MKT BUY → expect execDetailsEvent + commissionReportEvent webhooks.

    Skips when the market is closed (orders don't fill → no execution events).
    """
    baseline = len(webhook_payloads)

    resp = api.post(
        "/ibkr/order",
        json={
            "contract": {"symbol": "AAPL"},
            "order": {"action": "BUY", "totalQuantity": 1, "orderType": "MKT"},
        },
    )
    assert resp.status_code == 200, resp.text

    order_id = resp.json()["orderId"]
    filled = _wait_for_fill(api, order_id, timeout=10)
    if not filled:
        pytest.skip("Market appears closed — order did not fill, no execution events expected")

    # Wait for at least 2 webhooks (exec + commission)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if len(webhook_payloads) >= baseline + 2:
            break
        time.sleep(0.5)

    new = webhook_payloads[baseline:]
    assert len(new) >= 2, f"Expected >= 2 webhooks, got {len(new)}"

    # Verify payload structure
    for entry in new:
        payload = entry["body"]
        assert "trades" in payload
        assert "errors" in payload
        assert len(payload["trades"]) == 1

        trade = payload["trades"][0]
        assert trade["source"] in ("execDetailsEvent", "commissionReportEvent")
        assert trade["symbol"] == "AAPL"
        assert trade["side"] == "buy"

    # Both event types must be present
    sources = {e["body"]["trades"][0]["source"] for e in new}
    assert "execDetailsEvent" in sources, f"Missing execDetailsEvent, got: {sources}"
    assert "commissionReportEvent" in sources, f"Missing commissionReportEvent, got: {sources}"


def test_webhook_hmac_signature(
    webhook_payloads: list[dict[str, Any]],
) -> None:
    """Verify all received webhooks have valid HMAC-SHA256 signatures."""
    if len(webhook_payloads) == 0:
        pytest.skip("No webhooks received (market likely closed)")

    for entry in webhook_payloads:
        expected = "sha256=" + hmac.new(
            WEBHOOK_SECRET.encode(), entry["raw"], hashlib.sha256,
        ).hexdigest()
        assert hmac.compare_digest(entry["signature"], expected), (
            f"HMAC mismatch: got {entry['signature']!r}"
        )


def test_commission_report_has_commission(
    webhook_payloads: list[dict[str, Any]],
) -> None:
    """The commissionReportEvent webhook should include fee > 0."""
    commission_entries = [
        e for e in webhook_payloads
        if e["body"]["trades"][0]["source"] == "commissionReportEvent"
    ]
    if len(commission_entries) == 0:
        pytest.skip("No commissionReportEvent webhooks received (market likely closed)")

    for entry in commission_entries:
        trade = entry["body"]["trades"][0]
        assert trade["fee"] > 0, f"Expected fee > 0, got {trade['fee']}"


def test_debounce_path_fires_webhook(
    api: httpx.Client, webhook_payloads: list[dict[str, Any]],
) -> None:
    """With LISTENER_EVENT_DEBOUNCE_TIME=2000, commissionReportEvent goes through
    the debounce path: enqueue → timer → flush → aggregate → webhook.

    Verify the webhook arrives after at least ~2s (the debounce window) and
    contains the expected aggregated fields (execIds, fillCount).
    """
    baseline = len(webhook_payloads)

    resp = api.post(
        "/ibkr/order",
        json={
            "contract": {"symbol": "AAPL"},
            "order": {"action": "BUY", "totalQuantity": 1, "orderType": "MKT"},
        },
    )
    assert resp.status_code == 200, resp.text

    order_id = resp.json()["orderId"]
    filled = _wait_for_fill(api, order_id, timeout=10)
    if not filled:
        pytest.skip("Market appears closed — order did not fill")

    # execDetailsEvent arrives immediately (no debounce) — filter by THIS order
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        exec_events = [
            e for e in webhook_payloads[baseline:]
            if e["body"]["trades"][0]["source"] == "execDetailsEvent"
            and str(e["body"]["trades"][0]["orderId"]) == str(order_id)
        ]
        if exec_events:
            break
        time.sleep(0.3)
    assert exec_events, "execDetailsEvent webhook never arrived"

    # commissionReportEvent should arrive after debounce (~2s window from fill)
    # Filter by THIS order's orderId to avoid picking up stale events from
    # prior tests whose debounced webhooks may arrive after our baseline.
    deadline = time.monotonic() + 10
    commission_event = None
    while time.monotonic() < deadline:
        for e in webhook_payloads[baseline:]:
            t = e["body"]["trades"][0]
            if (t["source"] == "commissionReportEvent"
                    and str(t["orderId"]) == str(order_id)):
                commission_event = e
                break
        if commission_event:
            break
        time.sleep(0.3)

    assert commission_event is not None, "commissionReportEvent webhook never arrived"

    # The debounce path produces aggregated fields
    trade = commission_event["body"]["trades"][0]
    assert "execIds" in trade, "Debounced webhook missing execIds"
    assert isinstance(trade["execIds"], list)
    assert len(trade["execIds"]) >= 1
    assert "fillCount" in trade
    assert trade["fillCount"] >= 1
    assert trade["symbol"] == "AAPL"
    assert trade["side"] == "buy"

    # Measure the gap between execDetailsEvent and commissionReportEvent arrival
    # times. The /ibkr/order API blocks until IB fills the order, so t0-based
    # timing is unreliable. Instead, the gap between the two webhook arrivals
    # approximates the debounce window (2s) because execDetailsEvent fires
    # immediately while commissionReportEvent is buffered.
    exec_arrived = exec_events[0]["received_at"]
    comm_arrived = commission_event["received_at"]
    gap = comm_arrived - exec_arrived
    assert gap >= 1.5, (
        f"commissionReportEvent arrived only {gap:.1f}s after execDetailsEvent, "
        "debounce path may not have been used (expected >= 1.5s for 2s debounce)"
    )
