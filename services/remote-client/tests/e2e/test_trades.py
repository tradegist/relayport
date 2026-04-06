"""E2E tests — POST /ibkr/order and GET /ibkr/trades against a paper account."""

import time
from typing import Any

import httpx
import pytest

# ── Auth ─────────────────────────────────────────────────────────────


def test_trades_requires_auth(anon_api: httpx.Client) -> None:
    resp = anon_api.get("/ibkr/trades")
    assert resp.status_code == 401


# ── Validation ───────────────────────────────────────────────────────


def test_invalid_symbol_rejected(api: httpx.Client) -> None:
    """Bogus symbol either fails qualification (400/500) or is immediately
    cancelled by the exchange (200 with status Cancelled).  Paper-mode IB
    Gateway is very permissive with qualification, so we accept both."""
    resp = api.post(
        "/ibkr/order",
        json={
            "contract": {"symbol": "ZZZZZZ999", "exchange": "DOESNOTEXIST"},
            "order": {"action": "BUY", "totalQuantity": 1, "orderType": "MKT"},
        },
    )
    if resp.status_code == 200:
        # Paper mode qualified it but exchange cancelled immediately
        assert resp.json()["status"] == "Cancelled"
    else:
        assert resp.status_code in (400, 500)


# ── Order placement + trade listing ──────────────────────────────────


def _wait_for_trade(
    api: httpx.Client, order_id: int, timeout: float = 10,
) -> dict[str, Any]:
    """Poll /ibkr/trades until a trade with the given orderId appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = api.get("/ibkr/trades")
        assert resp.status_code == 200
        trades: list[dict[str, Any]] = resp.json()["trades"]
        match = [t for t in trades if t["orderId"] == order_id]
        if match:
            return match[0]
        time.sleep(0.5)
    pytest.fail(f"Trade with orderId={order_id} not found within {timeout}s")


def test_market_buy_appears_in_trades(api: httpx.Client) -> None:
    """Place MKT BUY → verify it appears in trades with fills."""
    order_resp = api.post(
        "/ibkr/order",
        json={
            "contract": {"symbol": "AAPL"},
            "order": {"action": "BUY", "totalQuantity": 1, "orderType": "MKT"},
        },
    )
    assert order_resp.status_code == 200, order_resp.text
    order_id = order_resp.json()["orderId"]

    trade = _wait_for_trade(api, order_id)
    assert trade["action"] == "BUY"
    assert trade["symbol"] == "AAPL"
    assert trade["orderType"] == "MKT"
    assert trade["totalQuantity"] == 1.0
    assert trade["status"] in ("Filled", "PreSubmitted", "Submitted")
    # MKT orders on paper fill immediately — verify fill details
    if trade["status"] == "Filled":
        assert len(trade["fills"]) >= 1
        fill = trade["fills"][0]
        assert fill["shares"] > 0
        assert fill["price"] > 0


def test_limit_buy_below_market(api: httpx.Client) -> None:
    """LMT BUY at $1 should be accepted but not fill."""
    order_resp = api.post(
        "/ibkr/order",
        json={
            "contract": {"symbol": "AAPL"},
            "order": {
                "action": "BUY",
                "totalQuantity": 1,
                "orderType": "LMT",
                "lmtPrice": 1.0,
            },
        },
    )
    assert order_resp.status_code == 200, order_resp.text
    data = order_resp.json()
    assert data["orderType"] == "LMT"
    assert data["lmtPrice"] == 1.0
    order_id = data["orderId"]

    trade = _wait_for_trade(api, order_id)
    assert trade["lmtPrice"] == 1.0
    # Should NOT have filled at $1
    assert trade["status"] in ("Submitted", "PreSubmitted")
    assert trade["filled"] == 0.0


def test_market_sell_appears_in_trades(api: httpx.Client) -> None:
    """Create an AAPL position, then sell it and verify the trade appears."""
    # First, establish a position to sell
    buy_resp = api.post(
        "/ibkr/order",
        json={
            "contract": {"symbol": "AAPL"},
            "order": {"action": "BUY", "totalQuantity": 1, "orderType": "MKT"},
        },
    )
    assert buy_resp.status_code == 200, buy_resp.text
    _wait_for_trade(api, buy_resp.json()["orderId"])

    # Now sell
    order_resp = api.post(
        "/ibkr/order",
        json={
            "contract": {"symbol": "AAPL"},
            "order": {"action": "SELL", "totalQuantity": 1, "orderType": "MKT"},
        },
    )
    assert order_resp.status_code == 200, order_resp.text
    order_id = order_resp.json()["orderId"]

    trade = _wait_for_trade(api, order_id)
    assert trade["action"] == "SELL"
    assert trade["symbol"] == "AAPL"


def test_trades_list_stable(api: httpx.Client) -> None:
    """Two consecutive GET /ibkr/trades return the same set of orderIds."""
    resp1 = api.get("/ibkr/trades")
    assert resp1.status_code == 200
    resp2 = api.get("/ibkr/trades")
    assert resp2.status_code == 200

    ids1 = {t["orderId"] for t in resp1.json()["trades"]}
    ids2 = {t["orderId"] for t in resp2.json()["trades"]}
    assert ids1 == ids2
