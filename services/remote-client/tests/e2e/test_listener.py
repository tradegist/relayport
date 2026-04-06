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
        assert trade["buySell"] == "BUY"

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
    """The commissionReportEvent webhook should include commission > 0."""
    commission_entries = [
        e for e in webhook_payloads
        if e["body"]["trades"][0]["source"] == "commissionReportEvent"
    ]
    if len(commission_entries) == 0:
        pytest.skip("No commissionReportEvent webhooks received (market likely closed)")

    for entry in commission_entries:
        trade = entry["body"]["trades"][0]
        assert trade["commission"] > 0, f"Expected commission > 0, got {trade['commission']}"
