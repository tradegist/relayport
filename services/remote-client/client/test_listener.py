"""Unit tests for client/listener.py."""

import asyncio
import sqlite3
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from client.listener import ListenerNamespace, _fill_to_trade, _map_to_fill
from models_poller import BuySell, Fill, WebhookPayloadTrades

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
#  _map_to_fill
# ═════════════════════════════════════════════════════════════════════════

class TestMapToFillExecDetails:
    """Mapping on execDetailsEvent — no commission data."""

    def test_basic_fields(self) -> None:
        ib_trade = _mock_ib_trade(symbol="TSLA", permId=42)
        fill = _mock_fill(shares=5.0, price=200.0)
        f = _map_to_fill(ib_trade, fill, "execDetailsEvent")

        assert isinstance(f, Fill)
        assert f.source == "execDetailsEvent"
        assert f.symbol == "TSLA"
        assert f.orderId == "42"
        assert f.volume == 5.0
        assert f.price == 200.0
        assert f.side == BuySell.BUY

    def test_commission_zero_on_exec_details(self) -> None:
        f = _map_to_fill(_mock_ib_trade(), _mock_fill(), "execDetailsEvent")
        assert f.fee == 0.0
        assert f.raw["commissionCurrency"] == ""
        assert f.raw["fifoPnlRealized"] == 0.0

    def test_sell_side_mapping(self) -> None:
        fill = _mock_fill(side="SLD")
        f = _map_to_fill(_mock_ib_trade(), fill, "execDetailsEvent")
        assert f.side == BuySell.SELL

    def test_unknown_side_raises(self) -> None:
        fill = _mock_fill(side="UNKNOWN")
        with pytest.raises(ValueError, match="Unknown execution side"):
            _map_to_fill(_mock_ib_trade(), fill, "execDetailsEvent")

    def test_empty_side_raises(self) -> None:
        fill = _mock_fill(side="")
        with pytest.raises(ValueError, match="Unknown execution side"):
            _map_to_fill(_mock_ib_trade(), fill, "execDetailsEvent")

    def test_datetime_iso_format(self) -> None:
        dt = datetime(2026, 4, 6, 14, 30, 0, tzinfo=UTC)
        fill = _mock_fill(time=dt)
        f = _map_to_fill(_mock_ib_trade(), fill, "execDetailsEvent")
        assert f.timestamp == "2026-04-06T14:30:00+00:00"

    def test_datetime_none(self) -> None:
        fill = _mock_fill(time=None)
        f = _map_to_fill(_mock_ib_trade(), fill, "execDetailsEvent")
        assert f.timestamp == ""

    def test_account_id(self) -> None:
        fill = _mock_fill(acctNumber="DU12345")
        f = _map_to_fill(_mock_ib_trade(), fill, "execDetailsEvent")
        assert f.raw["accountId"] == "DU12345"

    def test_contract_fields(self) -> None:
        ib_trade = _mock_ib_trade(secType="OPT", currency="EUR")
        f = _map_to_fill(ib_trade, _mock_fill(), "execDetailsEvent")
        assert f.raw["assetCategory"] == "OPT"
        assert f.raw["currency"] == "EUR"


class TestMapToFillCommissionReport:
    """Mapping on commissionReportEvent — includes commission data."""

    def test_commission_populated(self) -> None:
        cr = _mock_commission_report(commission=2.50, currency="USD", realizedPNL=15.0)
        fill = _mock_fill(commissionReport=cr)
        f = _map_to_fill(_mock_ib_trade(), fill, "commissionReportEvent")

        assert f.source == "commissionReportEvent"
        assert f.fee == 2.50
        assert f.raw["commissionCurrency"] == "USD"
        assert f.raw["fifoPnlRealized"] == 15.0

    def test_unset_sentinel_treated_as_zero(self) -> None:
        """ib_async uses UNSET_DOUBLE (1.7976...e308) for unset values."""
        cr = _mock_commission_report(
            commission=1.7976931348623157e308,
            realizedPNL=1.7976931348623157e308,
        )
        fill = _mock_fill(commissionReport=cr)
        f = _map_to_fill(_mock_ib_trade(), fill, "commissionReportEvent")
        assert f.fee == 0.0
        assert f.raw["fifoPnlRealized"] == 0.0


class TestFillToTrade:
    """_fill_to_trade wraps a Fill in a 1-fill Trade."""

    def test_wraps_fill(self) -> None:
        f = _map_to_fill(_mock_ib_trade(symbol="NVDA"), _mock_fill(execId="E1"), "commissionReportEvent")
        t = _fill_to_trade(f)
        assert t.symbol == "NVDA"
        assert t.execIds == ["E1"]
        assert t.fillCount == 1


# ═════════════════════════════════════════════════════════════════════════
#  ListenerNamespace — immediate mode (debounce_ms=0)
# ═════════════════════════════════════════════════════════════════════════

def _dedup_db() -> sqlite3.Connection:
    """In-memory dedup database for tests."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS processed_fills ("
        "  exec_id TEXT PRIMARY KEY,"
        "  processed_at TEXT DEFAULT (datetime('now'))"
        ")"
    )
    conn.commit()
    return conn


class TestListenerStart:
    def test_subscribes_events(self) -> None:
        ib = MagicMock()
        exec_event = ib.execDetailsEvent
        comm_event = ib.commissionReportEvent
        ns = ListenerNamespace(ib, notifiers=[], db=_dedup_db())

        loop = asyncio.new_event_loop()
        try:
            loop.call_soon(ns.start)
            loop.run_until_complete(asyncio.sleep(0.05))
        finally:
            loop.close()

        exec_event.__iadd__.assert_called_once_with(ns._on_exec_details)
        comm_event.__iadd__.assert_called_once_with(ns._on_commission_report)


class TestListenerDispatchImmediate:
    @patch("client.listener.notify")
    def test_exec_details_dispatches(self, mock_notify: MagicMock) -> None:
        ib = MagicMock()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()], db=_dedup_db())

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
        assert isinstance(payload, WebhookPayloadTrades)
        assert payload.data[0].source == "execDetailsEvent"
        assert payload.data[0].symbol == "AAPL"

    @patch("client.listener.notify")
    def test_commission_report_dispatches(self, mock_notify: MagicMock) -> None:
        ib = MagicMock()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()], db=_dedup_db())

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
        assert isinstance(payload, WebhookPayloadTrades)
        assert payload.data[0].source == "commissionReportEvent"
        assert payload.data[0].fee == 1.5

    @patch("client.listener.notify")
    def test_commission_report_dedup_skips_duplicate(self, mock_notify: MagicMock) -> None:
        """Second commissionReportEvent with same execId is skipped."""
        ib = MagicMock()
        db = _dedup_db()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()], db=db)

        ib_trade = _mock_ib_trade(symbol="TSLA")
        cr = _mock_commission_report(commission=1.5)
        fill = _mock_fill(commissionReport=cr, execId="DUP001")

        loop = asyncio.new_event_loop()
        try:
            # First call — should dispatch
            loop.call_soon(lambda: ns._on_commission_report(ib_trade, fill, cr))
            loop.run_until_complete(asyncio.sleep(0.2))
            assert mock_notify.call_count == 1

            # Second call — same execId, should be skipped
            loop.call_soon(lambda: ns._on_commission_report(ib_trade, fill, cr))
            loop.run_until_complete(asyncio.sleep(0.2))
            assert mock_notify.call_count == 1  # still 1, no second dispatch
        finally:
            loop.close()


# ═════════════════════════════════════════════════════════════════════════
#  ListenerNamespace — debounce mode (debounce_ms > 0)
# ═════════════════════════════════════════════════════════════════════════

class TestDebounceZeroDispatchesImmediately:
    """When debounce_ms=0, commissionReportEvent dispatches right away."""

    @patch("client.listener.notify")
    def test_no_debounce(self, mock_notify: MagicMock) -> None:
        ib = MagicMock()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()], db=_dedup_db(), debounce_ms=0)

        ib_trade = _mock_ib_trade(symbol="TSLA")
        cr = _mock_commission_report()
        fill = _mock_fill(commissionReport=cr, execId="EXEC1")

        loop = asyncio.new_event_loop()
        try:
            loop.call_soon(lambda: ns._on_commission_report(ib_trade, fill, cr))
            loop.run_until_complete(asyncio.sleep(0.2))
        finally:
            loop.close()

        mock_notify.assert_called_once()
        assert ns._pending == {}
        assert ns._timers == {}


class TestDebounceAggregatesRapidFills:
    """Multiple rapid fills for the same orderId are aggregated into one webhook."""

    @patch("client.listener.notify")
    def test_two_fills_one_trade(self, mock_notify: MagicMock) -> None:
        ib = MagicMock()
        db = _dedup_db()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()], db=db, debounce_ms=100)

        ib_trade = _mock_ib_trade(symbol="NVDA", permId=42)
        cr1 = _mock_commission_report(commission=0.50)
        cr2 = _mock_commission_report(commission=0.75)
        fill1 = _mock_fill(commissionReport=cr1, execId="E1", shares=30.0, price=100.0)
        fill2 = _mock_fill(commissionReport=cr2, execId="E2", shares=70.0, price=101.0)

        loop = asyncio.new_event_loop()
        try:
            loop.call_soon(lambda: ns._on_commission_report(ib_trade, fill1, cr1))
            loop.call_soon(lambda: ns._on_commission_report(ib_trade, fill2, cr2))
            # Wait for debounce to flush (100ms + margin)
            loop.run_until_complete(asyncio.sleep(0.3))
        finally:
            loop.close()

        mock_notify.assert_called_once()
        payload = mock_notify.call_args[0][1]
        trade = payload.data[0]
        assert trade.symbol == "NVDA"
        assert trade.volume == 100.0
        assert trade.fillCount == 2
        assert set(trade.execIds) == {"E1", "E2"}
        # Weighted average: (30*100 + 70*101) / 100 = 100.7
        assert abs(trade.price - 100.7) < 0.01
        # Commission summed
        assert abs(trade.fee - 1.25) < 0.01


class TestDebounceTimerResets:
    """A new fill for the same orderId resets the debounce timer."""

    @patch("client.listener.notify")
    def test_timer_reset(self, mock_notify: MagicMock) -> None:
        ib = MagicMock()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()], db=_dedup_db(), debounce_ms=200)

        ib_trade = _mock_ib_trade(permId=7)
        cr = _mock_commission_report()

        loop = asyncio.new_event_loop()
        try:
            # First fill at t=0
            fill1 = _mock_fill(commissionReport=cr, execId="R1", shares=10.0, price=50.0)
            loop.call_soon(lambda: ns._on_commission_report(ib_trade, fill1, cr))
            # Wait 150ms (< 200ms debounce) — timer not yet fired
            loop.run_until_complete(asyncio.sleep(0.15))
            assert mock_notify.call_count == 0

            # Second fill at t=150ms — resets the 200ms timer
            fill2 = _mock_fill(commissionReport=cr, execId="R2", shares=20.0, price=51.0)
            loop.call_soon(lambda: ns._on_commission_report(ib_trade, fill2, cr))
            # Wait another 150ms (total 300ms, but timer reset at 150ms → fires at 350ms)
            loop.run_until_complete(asyncio.sleep(0.15))
            assert mock_notify.call_count == 0  # still not flushed

            # Wait for flush (200ms from second fill)
            loop.run_until_complete(asyncio.sleep(0.15))
            assert mock_notify.call_count == 1
        finally:
            loop.close()


class TestDebounceDifferentOrdersIndependent:
    """Fills for different orderIds are debounced independently."""

    @patch("client.listener.notify")
    def test_independent_orders(self, mock_notify: MagicMock) -> None:
        ib = MagicMock()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()], db=_dedup_db(), debounce_ms=100)

        trade_a = _mock_ib_trade(symbol="AAPL", permId=1)
        trade_b = _mock_ib_trade(symbol="TSLA", permId=2)
        cr = _mock_commission_report()
        fill_a = _mock_fill(commissionReport=cr, execId="A1", shares=10.0)
        fill_b = _mock_fill(commissionReport=cr, execId="B1", shares=20.0)

        loop = asyncio.new_event_loop()
        try:
            loop.call_soon(lambda: ns._on_commission_report(trade_a, fill_a, cr))
            loop.call_soon(lambda: ns._on_commission_report(trade_b, fill_b, cr))
            loop.run_until_complete(asyncio.sleep(0.3))
        finally:
            loop.close()

        assert mock_notify.call_count == 2
        symbols = {mock_notify.call_args_list[i][0][1].data[0].symbol for i in range(2)}
        assert symbols == {"AAPL", "TSLA"}


class TestDebounceEnqueueDedup:
    """Duplicate execIds within the same debounce window are deduplicated on enqueue."""

    @patch("client.listener.notify")
    def test_same_exec_id_enqueued_twice(self, mock_notify: MagicMock) -> None:
        """Same commissionReportEvent arriving twice within the window produces 1 fill."""
        ib = MagicMock()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()], db=_dedup_db(), debounce_ms=100)

        ib_trade = _mock_ib_trade(symbol="NVDA", permId=10)
        cr = _mock_commission_report(commission=0.50)
        fill = _mock_fill(commissionReport=cr, execId="DUP1", shares=10.0, price=100.0)

        loop = asyncio.new_event_loop()
        try:
            # Enqueue the same fill twice (e.g. reconnect replay within window)
            loop.call_soon(lambda: ns._on_commission_report(ib_trade, fill, cr))
            loop.call_soon(lambda: ns._on_commission_report(ib_trade, fill, cr))
            loop.run_until_complete(asyncio.sleep(0.3))
        finally:
            loop.close()

        mock_notify.assert_called_once()
        payload = mock_notify.call_args[0][1]
        trade = payload.data[0]
        assert trade.fillCount == 1
        assert trade.execIds == ["DUP1"]
        assert trade.volume == 10.0


class TestDebounceDedup:
    """Debounce flush filters out already-processed fills."""

    @patch("client.listener.notify")
    def test_dedup_filters_already_seen(self, mock_notify: MagicMock) -> None:
        db = _dedup_db()
        # Pre-mark E1 as processed
        db.execute("INSERT INTO processed_fills (exec_id) VALUES (?)", ("E1",))
        db.commit()

        ib = MagicMock()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()], db=db, debounce_ms=100)

        ib_trade = _mock_ib_trade(symbol="NVDA", permId=5)
        cr = _mock_commission_report()
        fill1 = _mock_fill(commissionReport=cr, execId="E1", shares=10.0, price=100.0)
        fill2 = _mock_fill(commissionReport=cr, execId="E2", shares=20.0, price=101.0)

        loop = asyncio.new_event_loop()
        try:
            loop.call_soon(lambda: ns._on_commission_report(ib_trade, fill1, cr))
            loop.call_soon(lambda: ns._on_commission_report(ib_trade, fill2, cr))
            loop.run_until_complete(asyncio.sleep(0.3))
        finally:
            loop.close()

        mock_notify.assert_called_once()
        payload = mock_notify.call_args[0][1]
        trade = payload.data[0]
        # Only E2 should be in the trade (E1 filtered out)
        assert trade.fillCount == 1
        assert trade.execIds == ["E2"]
        assert trade.volume == 20.0

    @patch("client.listener.notify")
    def test_all_fills_already_seen_no_dispatch(self, mock_notify: MagicMock) -> None:
        db = _dedup_db()
        db.execute("INSERT INTO processed_fills (exec_id) VALUES (?)", ("E1",))
        db.execute("INSERT INTO processed_fills (exec_id) VALUES (?)", ("E2",))
        db.commit()

        ib = MagicMock()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()], db=db, debounce_ms=100)

        ib_trade = _mock_ib_trade(permId=5)
        cr = _mock_commission_report()
        fill1 = _mock_fill(commissionReport=cr, execId="E1")
        fill2 = _mock_fill(commissionReport=cr, execId="E2")

        loop = asyncio.new_event_loop()
        try:
            loop.call_soon(lambda: ns._on_commission_report(ib_trade, fill1, cr))
            loop.call_soon(lambda: ns._on_commission_report(ib_trade, fill2, cr))
            loop.run_until_complete(asyncio.sleep(0.3))
        finally:
            loop.close()

        mock_notify.assert_not_called()


class TestDebounceMarksProcessed:
    """After flushing, fills are marked as processed in the DB."""

    @patch("client.listener.notify")
    def test_marks_after_dispatch(self, mock_notify: MagicMock) -> None:
        db = _dedup_db()
        ib = MagicMock()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()], db=db, debounce_ms=100)

        ib_trade = _mock_ib_trade(permId=9)
        cr = _mock_commission_report()
        fill = _mock_fill(commissionReport=cr, execId="MARK1")

        loop = asyncio.new_event_loop()
        try:
            loop.call_soon(lambda: ns._on_commission_report(ib_trade, fill, cr))
            loop.run_until_complete(asyncio.sleep(0.3))
        finally:
            loop.close()

        mock_notify.assert_called_once()
        # Verify it's now in the DB
        row = db.execute(
            "SELECT 1 FROM processed_fills WHERE exec_id = ?", ("MARK1",)
        ).fetchone()
        assert row is not None


class TestDebouncePendingCleanup:
    """After flush, _pending and _timers are cleaned up."""

    @patch("client.listener.notify")
    def test_cleanup(self, mock_notify: MagicMock) -> None:
        ib = MagicMock()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()], db=_dedup_db(), debounce_ms=100)

        ib_trade = _mock_ib_trade(permId=3)
        cr = _mock_commission_report()
        fill = _mock_fill(commissionReport=cr, execId="C1")

        loop = asyncio.new_event_loop()
        try:
            loop.call_soon(lambda: ns._on_commission_report(ib_trade, fill, cr))
            loop.run_until_complete(asyncio.sleep(0.3))
        finally:
            loop.close()

        assert ns._pending == {}
        assert ns._timers == {}


class TestDebounceExecDetailsNotDebounced:
    """execDetailsEvent is never debounced, even when debounce_ms > 0."""

    @patch("client.listener.notify")
    def test_exec_details_immediate(self, mock_notify: MagicMock) -> None:
        ib = MagicMock()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()], db=_dedup_db(), debounce_ms=500)

        ib_trade = _mock_ib_trade(symbol="AAPL")
        fill = _mock_fill()

        loop = asyncio.new_event_loop()
        try:
            loop.call_soon(lambda: ns._on_exec_details(ib_trade, fill))
            loop.run_until_complete(asyncio.sleep(0.2))
        finally:
            loop.close()

        mock_notify.assert_called_once()
        assert ns._pending == {}


class TestNotifierFailure:
    def test_dispatch_does_not_raise_on_notifier_error(self) -> None:
        """If notify() raises, the event loop must not crash."""
        ib = MagicMock()
        ns = ListenerNamespace(ib, notifiers=[MagicMock()], db=_dedup_db())

        f = _map_to_fill(_mock_ib_trade(), _mock_fill(), "execDetailsEvent")
        trade = _fill_to_trade(f)

        loop = asyncio.new_event_loop()
        try:
            with patch("client.listener.notify", side_effect=RuntimeError("boom")):
                loop.call_soon(lambda: ns._dispatch(trade))
                loop.run_until_complete(asyncio.sleep(0.2))
            # If we get here without exception, the test passes
        finally:
            loop.close()
