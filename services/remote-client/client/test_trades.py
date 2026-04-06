"""Unit tests for client/trades.py — mapping helpers and deduplication."""

import asyncio
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from client.trades import TradesNamespace, _lmt_price, _map_fill, _map_trade

# ── _lmt_price sentinel handling ─────────────────────────────────────


class TestLmtPrice:
    def test_none_returns_none(self) -> None:
        assert _lmt_price(None) is None

    def test_unset_sentinel_returns_none(self) -> None:
        assert _lmt_price(1.7976931348623157e308) is None

    def test_normal_float(self) -> None:
        assert _lmt_price(150.50) == 150.50

    def test_zero_is_valid(self) -> None:
        assert _lmt_price(0.0) == 0.0

    def test_decimal_converted_to_float(self) -> None:
        result = _lmt_price(Decimal("123.45"))
        assert result == 123.45
        assert isinstance(result, float)


# ── _map_fill ────────────────────────────────────────────────────────


def _mock_fill(
    exec_id: str = "0001",
    time: datetime | None = None,
    exchange: str = "SMART",
    side: str = "BOT",
    shares: float = 10.0,
    price: float = 150.0,
    commission: float = 1.0,
    currency: str = "USD",
    realized_pnl: float = 0.0,
) -> MagicMock:
    fill = MagicMock()
    fill.execution.execId = exec_id
    fill.execution.time = time or datetime(2026, 1, 15, 10, 30, 0)
    fill.execution.exchange = exchange
    fill.execution.side = side
    fill.execution.shares = shares
    fill.execution.price = price
    fill.commissionReport.commission = commission
    fill.commissionReport.currency = currency
    fill.commissionReport.realizedPNL = realized_pnl
    return fill


class TestMapFill:
    def test_basic_mapping(self) -> None:
        fill = _mock_fill(exec_id="EX001", price=155.0, shares=5.0)
        detail = _map_fill(fill)
        assert detail.execId == "EX001"
        assert detail.price == 155.0
        assert detail.shares == 5.0
        assert detail.side == "BOT"
        assert detail.commissionCurrency == "USD"

    def test_time_iso_format(self) -> None:
        dt = datetime(2026, 4, 4, 14, 30, 0)
        fill = _mock_fill(time=dt)
        detail = _map_fill(fill)
        assert detail.time == "2026-04-04T14:30:00"

    def test_none_time_returns_empty_string(self) -> None:
        fill = _mock_fill()
        fill.execution.time = None
        detail = _map_fill(fill)
        assert detail.time == ""


# ── _map_trade ───────────────────────────────────────────────────────


def _mock_ib_trade(
    order_id: int = 1,
    perm_id: int = 100,
    action: str = "BUY",
    total_qty: float = 10.0,
    order_type: str = "MKT",
    lmt_price: float | None = None,
    tif: str = "DAY",
    symbol: str = "AAPL",
    sec_type: str = "STK",
    exchange: str = "SMART",
    currency: str = "USD",
    status: str = "Filled",
    filled: float = 10.0,
    remaining: float = 0.0,
    avg_fill_price: float = 150.0,
    fills: Sequence[object] | None = None,
) -> MagicMock:
    trade = MagicMock()
    trade.order.orderId = order_id
    trade.order.permId = perm_id
    trade.order.action = action
    trade.order.totalQuantity = total_qty
    trade.order.orderType = order_type
    trade.order.lmtPrice = lmt_price
    trade.order.tif = tif
    trade.contract.symbol = symbol
    trade.contract.secType = sec_type
    trade.contract.exchange = exchange
    trade.contract.currency = currency
    trade.orderStatus.status = status
    trade.orderStatus.filled = filled
    trade.orderStatus.remaining = remaining
    trade.orderStatus.avgFillPrice = avg_fill_price
    trade.fills = fills or []
    return trade


class TestMapTrade:
    def test_basic_mapping(self) -> None:
        t = _mock_ib_trade(order_id=42, perm_id=999, symbol="TSLA")
        detail = _map_trade(t)
        assert detail.orderId == 999
        assert detail.symbol == "TSLA"
        assert detail.status == "Filled"

    def test_lmt_price_unset_sentinel(self) -> None:
        t = _mock_ib_trade(lmt_price=1.7976931348623157e308)
        detail = _map_trade(t)
        assert detail.lmtPrice is None

    def test_lmt_price_present(self) -> None:
        t = _mock_ib_trade(order_type="LMT", lmt_price=150.0)
        detail = _map_trade(t)
        assert detail.lmtPrice == 150.0

    def test_fills_mapped(self) -> None:
        fills = [_mock_fill(exec_id="A"), _mock_fill(exec_id="B")]
        t = _mock_ib_trade(fills=fills)
        detail = _map_trade(t)
        assert len(detail.fills) == 2
        assert detail.fills[0].execId == "A"
        assert detail.fills[1].execId == "B"


# ── TradesNamespace.list() deduplication ─────────────────────────────


class TestDedup:
    def test_session_and_completed_deduped_by_perm_id(self) -> None:
        shared_trade = _mock_ib_trade(perm_id=100, symbol="AAPL")
        completed_dup = _mock_ib_trade(perm_id=100, symbol="AAPL")
        completed_new = _mock_ib_trade(perm_id=200, symbol="TSLA")

        ib = MagicMock()
        ib.trades.return_value = [shared_trade]
        ib.reqCompletedOrdersAsync = AsyncMock(
            return_value=[completed_dup, completed_new]
        )

        ns = TradesNamespace(ib)
        result = asyncio.run(ns.list())
        assert len(result.trades) == 2
        perm_ids = {t.orderId for t in result.trades}
        assert perm_ids == {100, 200}

    def test_session_trades_take_priority(self) -> None:
        """When session and completed have the same permId, session wins."""
        session = _mock_ib_trade(perm_id=100, status="Filled")
        completed = _mock_ib_trade(perm_id=100, status="Inactive")

        ib = MagicMock()
        ib.trades.return_value = [session]
        ib.reqCompletedOrdersAsync = AsyncMock(return_value=[completed])

        ns = TradesNamespace(ib)
        result = asyncio.run(ns.list())
        assert len(result.trades) == 1
        assert result.trades[0].status == "Filled"

    def test_empty_trades(self) -> None:
        ib = MagicMock()
        ib.trades.return_value = []
        ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])

        ns = TradesNamespace(ib)
        result = asyncio.run(ns.list())
        assert result.trades == []
