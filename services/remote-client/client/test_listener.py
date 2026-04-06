"""Unit tests for client/listener.py."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from client.listener import ListenerNamespace, _map_to_trade
from models_poller import BuySell, WebhookPayload

# ── Mock factories ───────────────────────────────────────────────────────

def _mock_execution(**overrides: Any) -> MagicMock:
    ex = MagicMock()
    ex.execId = overrides.get("execId", "0001a.00001.01.01")
    ex.side = overrides.get("side", "BOT")
    ex.shares = overrides.get("shares", 10.0)
    ex.price = overrides.get("price", 150.0)
    ex.exchange = overrides.get("exchange", "ISLAND")
    ex.time = overrides.get("time", datetime(2026, 4, 6, 14, 30, 0, tzinfo=UTC))
    ex.acctNumber = overrides.get("acctNumber", "UXXXXXXX")
    return ex


def _mock_commission_report(**overrides: Any) -> MagicMock:
    cr = MagicMock()
    cr.commission = overrides.get("commission", 1.25)
    cr.currency = overrides.get("currency", "USD")
    cr.realizedPNL = overrides.get("realizedPNL", 0.0)
    return cr


def _mock_fill(**overrides: Any) -> MagicMock:
    fill = MagicMock()
    fill.execution = _mock_execution(**{
        k: v for k, v in overrides.items()
        if k in {"execId", "side", "shares", "price", "exchange", "time", "acctNumber"}
    })
    fill.commissionReport = overrides.get(
        "commissionReport", _mock_commission_report(),
    )
    return fill


def _mock_ib_trade(**overrides: Any) -> MagicMock:
    trade = MagicMock()
    trade.order.permId = overrides.get("permId", 999)
    trade.order.action = overrides.get("action", "BUY")
    trade.contract.symbol = overrides.get("symbol", "AAPL")
    trade.contract.secType = overrides.get("secType", "STK")
    trade.contract.exchange = overrides.get("exchange", "SMART")
    trade.contract.currency = overrides.get("currency", "USD")
    return trade


# ═════════════════════════════════════════════════════════════════════════
#  _map_to_trade
# ═════════════════════════════════════════════════════════════════════════

class TestMapToTradeExecDetails:
    """Mapping on execDetailsEvent — no commission data."""

    def test_basic_fields(self) -> None:
        ib_trade = _mock_ib_trade(symbol="TSLA", permId=42)
        fill = _mock_fill(shares=5.0, price=200.0)
        t = _map_to_trade(ib_trade, fill, "execDetailsEvent")

        assert t.source == "execDetailsEvent"
        assert t.symbol == "TSLA"
        assert t.orderId == "42"
        assert t.quantity == 5.0
        assert t.price == 200.0
        assert t.buySell == BuySell.BUY
        assert t.fillCount == 1
        assert t.execIds == ["0001a.00001.01.01"]

    def test_commission_zero_on_exec_details(self) -> None:
        t = _map_to_trade(_mock_ib_trade(), _mock_fill(), "execDetailsEvent")
        assert t.commission == 0.0
        assert t.commissionCurrency == ""
        assert t.fifoPnlRealized == 0.0

    def test_sell_side_mapping(self) -> None:
        fill = _mock_fill(side="SLD")
        t = _map_to_trade(_mock_ib_trade(), fill, "execDetailsEvent")
        assert t.buySell == BuySell.SELL

    def test_unknown_side_defaults_buy(self) -> None:
        fill = _mock_fill(side="UNKNOWN")
        t = _map_to_trade(_mock_ib_trade(), fill, "execDetailsEvent")
        assert t.buySell == BuySell.BUY

    def test_datetime_iso_format(self) -> None:
        dt = datetime(2026, 4, 6, 14, 30, 0, tzinfo=UTC)
        fill = _mock_fill(time=dt)
        t = _map_to_trade(_mock_ib_trade(), fill, "execDetailsEvent")
        assert t.dateTime == "2026-04-06T14:30:00+00:00"

    def test_datetime_none(self) -> None:
        fill = _mock_fill(time=None)
        t = _map_to_trade(_mock_ib_trade(), fill, "execDetailsEvent")
        assert t.dateTime == ""

    def test_account_id(self) -> None:
        fill = _mock_fill(acctNumber="DU12345")
        t = _map_to_trade(_mock_ib_trade(), fill, "execDetailsEvent")
        assert t.accountId == "DU12345"

    def test_contract_fields(self) -> None:
        ib_trade = _mock_ib_trade(secType="OPT", currency="EUR")
        t = _map_to_trade(ib_trade, _mock_fill(), "execDetailsEvent")
        assert t.assetCategory == "OPT"
        assert t.currency == "EUR"


class TestMapToTradeCommissionReport:
    """Mapping on commissionReportEvent — includes commission data."""

    def test_commission_populated(self) -> None:
        cr = _mock_commission_report(commission=2.50, currency="USD", realizedPNL=15.0)
        fill = _mock_fill(commissionReport=cr)
        t = _map_to_trade(_mock_ib_trade(), fill, "commissionReportEvent")

        assert t.source == "commissionReportEvent"
        assert t.commission == 2.50
        assert t.commissionCurrency == "USD"
        assert t.fifoPnlRealized == 15.0

    def test_unset_sentinel_treated_as_zero(self) -> None:
        """ib_async uses UNSET_DOUBLE (1.7976...e308) for unset values."""
        cr = _mock_commission_report(
            commission=1.7976931348623157e308,
            realizedPNL=1.7976931348623157e308,
        )
        fill = _mock_fill(commissionReport=cr)
        t = _map_to_trade(_mock_ib_trade(), fill, "commissionReportEvent")
        assert t.commission == 0.0
        assert t.fifoPnlRealized == 0.0


# ═════════════════════════════════════════════════════════════════════════
#  ListenerNamespace
# ═════════════════════════════════════════════════════════════════════════

class TestListenerStart:
    def test_subscribes_events(self) -> None:
        ib = MagicMock()
        # Capture event mocks before += replaces them
        exec_event = ib.execDetailsEvent
        comm_event = ib.commissionReportEvent
        ns = ListenerNamespace(ib, notifiers=[])
        ns.start()
        exec_event.__iadd__.assert_called_once_with(ns._on_exec_details)
        comm_event.__iadd__.assert_called_once_with(ns._on_commission_report)


class TestListenerDispatch:
    @patch("notifier.notify")
    def test_exec_details_dispatches(self, mock_notify: MagicMock) -> None:
        ib = MagicMock()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()])

        ib_trade = _mock_ib_trade(symbol="AAPL")
        fill = _mock_fill()

        loop = asyncio.new_event_loop()
        try:
            loop.call_soon(lambda: ns._on_exec_details(ib_trade, fill))
            loop.run_until_complete(asyncio.sleep(0.2))
        finally:
            loop.close()

        mock_notify.assert_called_once()
        payload = mock_notify.call_args[0][1]
        assert isinstance(payload, WebhookPayload)
        assert payload.trades[0].source == "execDetailsEvent"
        assert payload.trades[0].symbol == "AAPL"

    @patch("notifier.notify")
    def test_commission_report_dispatches(self, mock_notify: MagicMock) -> None:
        ib = MagicMock()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()])

        ib_trade = _mock_ib_trade(symbol="TSLA")
        cr = _mock_commission_report(commission=1.5)
        fill = _mock_fill(commissionReport=cr)

        loop = asyncio.new_event_loop()
        try:
            loop.call_soon(lambda: ns._on_commission_report(ib_trade, fill, cr))
            loop.run_until_complete(asyncio.sleep(0.2))
        finally:
            loop.close()

        mock_notify.assert_called_once()
        payload = mock_notify.call_args[0][1]
        assert isinstance(payload, WebhookPayload)
        assert payload.trades[0].source == "commissionReportEvent"
        assert payload.trades[0].commission == 1.5


class TestNotifierFailure:
    def test_dispatch_does_not_raise_on_notifier_error(self) -> None:
        """If notify() raises, the event loop must not crash."""
        ib = MagicMock()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()])

        trade = _map_to_trade(_mock_ib_trade(), _mock_fill(), "execDetailsEvent")

        loop = asyncio.new_event_loop()
        try:
            with patch("notifier.notify", side_effect=RuntimeError("boom")):
                loop.call_soon(lambda: ns._dispatch(trade))
                loop.run_until_complete(asyncio.sleep(0.2))
            # If we get here without exception, the test passes
        finally:
            loop.close()
