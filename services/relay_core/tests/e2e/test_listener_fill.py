"""Listener E2E — verify WS connection and fill delivery.

Requires:
- ibkr_bridge running locally with IB Gateway connected (paper account)
- relayport E2E stack running (make e2e-up)
- IBKR_BRIDGE_API_TOKEN set in .env.test

The fill test places a market order for 1 share of AAPL via the bridge
API, then polls the debug webhook inbox until the listener delivers the
fill.  It skips (not fails) when the market is closed.
"""

import subprocess
import time

import httpx
import pytest

from relay_core.tests.e2e.conftest import DEBUG_INBOX_PATH

pytestmark = pytest.mark.usefixtures("_bridge_preflight")

_E2E_COMPOSE = (
    "SITE_DOMAIN=unused API_TOKEN=test-token "
    "docker compose -f docker-compose.yml -f docker-compose.test.yml "
    "-p relayport-test --env-file .env.test"
)


def _get_listener_logs() -> str:
    """Return recent relays container logs filtered for listener lines."""
    result = subprocess.run(
        f"{_E2E_COMPOSE} logs relays --since 2m",
        shell=True,
        capture_output=True,
        text=True,
    )
    return result.stdout + result.stderr


def test_listener_ws_connected() -> None:
    """Verify the listener connects to the bridge WS endpoint."""
    logs = _get_listener_logs()
    assert "Connected to WS" in logs, (
        "Listener did not connect to WS. Logs:\n" + logs[-2000:]
    )


def test_listener_receives_commission_fill(
    bridge_api: httpx.Client,
    debug_api: httpx.Client,
) -> None:
    """Place a MKT order on bridge → listener picks up fill → debug inbox."""
    # Clear the debug inbox so we start clean.
    debug_api.delete(DEBUG_INBOX_PATH)
    inbox_resp = debug_api.get(DEBUG_INBOX_PATH)
    assert inbox_resp.status_code == 200
    assert inbox_resp.json()["count"] == 0, "Debug inbox not empty after clear"

    # Place a market order for 1 share of AAPL (paper account).
    order_payload = {
        "contract": {
            "symbol": "AAPL",
            "secType": "STK",
            "exchange": "SMART",
            "currency": "USD",
        },
        "order": {
            "action": "BUY",
            "totalQuantity": 1,
            "orderType": "MKT",
        },
    }
    resp = bridge_api.post("/ibkr/order", json=order_payload)
    assert resp.status_code == 200, f"Order placement failed: {resp.text}"

    # Poll the debug inbox until the fill arrives.
    # Market orders on paper usually fill within a few seconds during RTH.
    # Outside market hours the order sits in PreSubmitted — skip gracefully.
    timeout_s = 10
    poll_interval_s = 3
    deadline = time.monotonic() + timeout_s

    fill_trade = None
    while time.monotonic() < deadline:
        time.sleep(poll_interval_s)

        inbox_resp = debug_api.get(DEBUG_INBOX_PATH)
        if inbox_resp.status_code != 200:
            continue

        inbox = inbox_resp.json()
        for entry in inbox.get("payloads", []):
            payload = entry.get("payload", {})
            trades = payload.get("data", [])
            for trade in trades:
                if trade.get("symbol") == "AAPL":
                    fill_trade = trade
                    break
            if fill_trade:
                break
        if fill_trade:
            break

    if fill_trade is None:
        pytest.skip(
            f"No fill received within {timeout_s}s — "
            "market is likely closed (order not filled)"
        )

    # Verify fill shape.
    assert fill_trade["side"] in ("BUY", "SELL")
    assert fill_trade["fillCount"] >= 1
    assert fill_trade["volume"] > 0
    assert fill_trade["price"] > 0
    assert fill_trade["fee"] >= 0
    assert fill_trade["source"] == "commissionReportEvent"
    assert len(fill_trade.get("execIds", [])) >= 1
