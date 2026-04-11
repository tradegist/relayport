"""Tests for the Bridge→Relay real-time listener."""

import asyncio
import os
import unittest
from typing import Any, cast
from unittest.mock import MagicMock, patch

from listener import (
    _handle_event,
    _send_and_mark,
    _send_no_mark,
    get_bridge_api_token,
    get_bridge_ws_url,
    get_debounce_ms,
    is_exec_events_enabled,
    is_listener_enabled,
    map_fill,
)
from listener.bridge_models import (
    WsCommissionReport,
    WsContract,
    WsEnvelope,
    WsEventType,
    WsExecution,
    WsFill,
)
from shared import BuySell

# ── Env var setup ────────────────────────────────────────────────────

_ORIG_ENV: dict[str, str | None] = {}
_TEST_ENV = {
    "BRIDGE_WS_URL": "ws://bridge:5000/ibkr/ws/events",
    "BRIDGE_API_TOKEN": "test-token",
    "LISTENER_ENABLED": "true",
    "LISTENER_EXEC_EVENTS_ENABLED": "false",
    "LISTENER_EVENT_DEBOUNCE_TIME": "0",
}


def setUpModule() -> None:
    for key, val in _TEST_ENV.items():
        _ORIG_ENV[key] = os.environ.get(key)
        os.environ[key] = val


def tearDownModule() -> None:
    for key, orig in _ORIG_ENV.items():
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig


# ── Defaults (type-checked by mypy at construction time) ────────────

_DEFAULT_CONTRACT = WsContract(
    secType="STK",
    conId=265598,
    symbol="AAPL",
    lastTradeDateOrContractMonth="",
    strike=0.0,
    right="",
    multiplier="",
    exchange="SMART",
    primaryExchange="NASDAQ",
    currency="USD",
    localSymbol="AAPL",
    tradingClass="AAPL",
    includeExpired=False,
    secIdType="",
    secId="",
    description="",
    issuerId="",
    comboLegsDescrip="",
)

_DEFAULT_EXECUTION = WsExecution(
    execId="0001",
    time="20260411-10:30:00",
    acctNumber="UXXXXXXX",
    exchange="ISLAND",
    side="BOT",
    shares=100.0,
    price=150.25,
    permId=12345,
    clientId=1,
    orderId=42,
    liquidation=0,
    cumQty=100.0,
    avgPrice=150.25,
    orderRef="",
    evRule="",
    evMultiplier=0.0,
    modelCode="",
    lastLiquidity=0,
    pendingPriceRevision=False,
)

_DEFAULT_COMMISSION = WsCommissionReport(
    execId="0001",
    commission=1.05,
    currency="USD",
    realizedPNL=0.0,
    yield_=0.0,
    yieldRedemptionDate=0,
)


def _make_contract(**overrides: Any) -> WsContract:
    return _DEFAULT_CONTRACT.model_copy(update=overrides)


def _make_execution(**overrides: Any) -> WsExecution:
    return _DEFAULT_EXECUTION.model_copy(update=overrides)


def _make_commission(**overrides: Any) -> WsCommissionReport:
    return _DEFAULT_COMMISSION.model_copy(update=overrides)


def _make_envelope(
    event_type: str = "commissionReportEvent",
    seq: int = 1,
    exec_id: str = "0001",
    side: str = "BOT",
    shares: float = 100.0,
    price: float = 150.25,
    commission: float = 1.05,
    symbol: str = "AAPL",
    sec_type: str = "STK",
    perm_id: int = 12345,
    has_fill: bool = True,
) -> WsEnvelope:
    fill: WsFill | None = None
    if has_fill:
        fill = WsFill(
            contract=_make_contract(symbol=symbol, secType=sec_type),
            execution=_make_execution(
                execId=exec_id, side=side, shares=shares,
                price=price, permId=perm_id,
            ),
            commissionReport=_make_commission(
                execId=exec_id, commission=commission,
            ),
            time="20260411-10:30:00",
        )
    return WsEnvelope(
        type=cast(WsEventType, event_type),
        seq=seq,
        timestamp="2026-04-11T10:30:00+00:00",
        fill=fill,
    )


# ── map_fill tests ───────────────────────────────────────────────────


class TestMapFill(unittest.TestCase):
    """Test WsEnvelope → relay Fill mapping."""

    def test_bot_maps_to_buy(self) -> None:
        envelope = _make_envelope(side="BOT")
        fill = map_fill(envelope)
        assert fill is not None
        self.assertEqual(fill.side, BuySell.BUY)

    def test_sld_maps_to_sell(self) -> None:
        envelope = _make_envelope(side="SLD")
        fill = map_fill(envelope)
        assert fill is not None
        self.assertEqual(fill.side, BuySell.SELL)

    def test_unknown_side_returns_none(self) -> None:
        envelope = _make_envelope(side="UNKNOWN")
        fill = map_fill(envelope)
        self.assertIsNone(fill)

    def test_no_fill_returns_none(self) -> None:
        envelope = _make_envelope(has_fill=False)
        fill = map_fill(envelope)
        self.assertIsNone(fill)

    def test_empty_exec_id_returns_none(self) -> None:
        envelope = _make_envelope(exec_id="")
        fill = map_fill(envelope)
        self.assertIsNone(fill)

    def test_whitespace_exec_id_returns_none(self) -> None:
        envelope = _make_envelope(exec_id="   ")
        fill = map_fill(envelope)
        self.assertIsNone(fill)

    def test_exec_id_is_stripped(self) -> None:
        envelope = _make_envelope(exec_id="  ABC123  ")
        fill = map_fill(envelope)
        assert fill is not None
        self.assertEqual(fill.execId, "ABC123")

    def test_fee_is_positive(self) -> None:
        envelope = _make_envelope(commission=-0.62)
        fill = map_fill(envelope)
        assert fill is not None
        self.assertEqual(fill.fee, 0.62)

    def test_cost_is_price_times_volume(self) -> None:
        envelope = _make_envelope(price=150.0, shares=10.0)
        fill = map_fill(envelope)
        assert fill is not None
        self.assertEqual(fill.cost, 1500.0)

    def test_order_type_is_none(self) -> None:
        envelope = _make_envelope()
        fill = map_fill(envelope)
        assert fill is not None
        self.assertIsNone(fill.orderType)

    def test_source_matches_event_type(self) -> None:
        envelope = _make_envelope(event_type="commissionReportEvent")
        fill = map_fill(envelope)
        assert fill is not None
        self.assertEqual(fill.source, "commissionReportEvent")

    def test_order_id_is_perm_id_string(self) -> None:
        envelope = _make_envelope(perm_id=99999)
        fill = map_fill(envelope)
        assert fill is not None
        self.assertEqual(fill.orderId, "99999")

    def test_asset_class_from_sec_type(self) -> None:
        envelope = _make_envelope(sec_type="OPT")
        fill = map_fill(envelope)
        assert fill is not None
        self.assertEqual(fill.assetClass, "option")

    def test_unknown_sec_type_maps_to_other(self) -> None:
        envelope = _make_envelope(sec_type="BOND")
        fill = map_fill(envelope)
        assert fill is not None
        self.assertEqual(fill.assetClass, "other")


# ── _send_and_mark tests ────────────────────────────────────────────


class TestSendAndMark(unittest.TestCase):
    """Test the dedup + aggregate + notify + mark pipeline."""

    @patch("listener.mark_processed_batch")
    @patch("listener.notify")
    @patch("listener.get_processed_ids", return_value=set())
    @patch("listener._init_dedup_db")
    def test_new_fill_dispatched_and_marked(
        self,
        mock_init_db: MagicMock,
        mock_get_ids: MagicMock,
        mock_notify: MagicMock,
        mock_mark: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_init_db.return_value = mock_conn

        envelope = _make_envelope()
        fill = map_fill(envelope)
        assert fill is not None

        _send_and_mark([fill], [], "/tmp/test.db")

        mock_notify.assert_called_once()
        mock_mark.assert_called_once()
        # Verify mark was called with the connection and exec IDs
        mark_args = mock_mark.call_args[0]
        self.assertEqual(mark_args[0], mock_conn)
        self.assertEqual(mark_args[1], ["0001"])
        mock_conn.close.assert_called_once()

    @patch("listener.mark_processed_batch")
    @patch("listener.notify")
    @patch("listener.get_processed_ids", return_value={"0001"})
    @patch("listener._init_dedup_db")
    def test_already_seen_fill_skipped(
        self,
        mock_init_db: MagicMock,
        mock_get_ids: MagicMock,
        mock_notify: MagicMock,
        mock_mark: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_init_db.return_value = mock_conn

        envelope = _make_envelope()
        fill = map_fill(envelope)
        assert fill is not None

        _send_and_mark([fill], [], "/tmp/test.db")

        mock_notify.assert_not_called()
        mock_mark.assert_not_called()
        mock_conn.close.assert_called_once()

    @patch("listener.mark_processed_batch")
    @patch("listener.notify")
    @patch("listener.get_processed_ids", return_value=set())
    @patch("listener._init_dedup_db")
    def test_connection_closed_on_error(
        self,
        mock_init_db: MagicMock,
        mock_get_ids: MagicMock,
        mock_notify: MagicMock,
        mock_mark: MagicMock,
    ) -> None:
        """Connection is closed even if notify raises."""
        mock_conn = MagicMock()
        mock_init_db.return_value = mock_conn
        mock_notify.side_effect = RuntimeError("boom")

        envelope = _make_envelope()
        fill = map_fill(envelope)
        assert fill is not None

        with self.assertRaises(RuntimeError):
            _send_and_mark([fill], [], "/tmp/test.db")

        mock_conn.close.assert_called_once()


# ── _send_no_mark tests ─────────────────────────────────────────────


class TestSendNoMark(unittest.TestCase):
    """Test fire-and-forget dispatch for exec events."""

    @patch("listener.notify")
    def test_dispatches_without_marking(self, mock_notify: MagicMock) -> None:
        envelope = _make_envelope(event_type="execDetailsEvent")
        fill = map_fill(envelope)
        assert fill is not None

        _send_no_mark([fill], [])

        mock_notify.assert_called_once()
        payload = mock_notify.call_args[0][1]
        self.assertEqual(len(payload.data), 1)
        self.assertEqual(payload.data[0].symbol, "AAPL")


# ── _handle_event tests ─────────────────────────────────────────────


class TestHandleEvent(unittest.IsolatedAsyncioTestCase):
    """Test event filtering and dispatch routing."""

    async def test_connected_event_ignored(self) -> None:
        """Status events are logged and not dispatched."""
        data = {"type": "connected", "seq": 1, "timestamp": "t", "fill": None}
        # Should not raise or call any dispatch functions
        await _handle_event(
            data, notifiers=[], exec_events_enabled=False,
            debounce_buf=None, db_path="/tmp/test.db",
        )

    async def test_disconnected_event_ignored(self) -> None:
        data = {"type": "disconnected", "seq": 2, "timestamp": "t", "fill": None}
        await _handle_event(
            data, notifiers=[], exec_events_enabled=False,
            debounce_buf=None, db_path="/tmp/test.db",
        )

    @patch("listener._send_and_mark")
    async def test_commission_event_dispatched(
        self, mock_send: MagicMock,
    ) -> None:
        envelope = _make_envelope(event_type="commissionReportEvent")
        data: dict[str, Any] = envelope.model_dump()

        await _handle_event(
            data, notifiers=[], exec_events_enabled=False,
            debounce_buf=None, db_path="/tmp/test.db",
        )

        mock_send.assert_called_once()
        fills = mock_send.call_args[0][0]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].execId, "0001")

    @patch("listener._send_no_mark")
    @patch("listener._send_and_mark")
    async def test_exec_event_skipped_when_disabled(
        self,
        mock_send_mark: MagicMock,
        mock_send_no: MagicMock,
    ) -> None:
        envelope = _make_envelope(event_type="execDetailsEvent")
        data: dict[str, Any] = envelope.model_dump()

        await _handle_event(
            data, notifiers=[], exec_events_enabled=False,
            debounce_buf=None, db_path="/tmp/test.db",
        )

        mock_send_no.assert_not_called()
        mock_send_mark.assert_not_called()

    @patch("listener._send_no_mark")
    async def test_exec_event_dispatched_when_enabled(
        self, mock_send_no: MagicMock,
    ) -> None:
        envelope = _make_envelope(event_type="execDetailsEvent")
        data: dict[str, Any] = envelope.model_dump()

        await _handle_event(
            data, notifiers=[], exec_events_enabled=True,
            debounce_buf=None, db_path="/tmp/test.db",
        )

        mock_send_no.assert_called_once()

    async def test_invalid_envelope_logged_not_raised(self) -> None:
        """Malformed data doesn't crash the handler."""
        data: dict[str, Any] = {"type": "commissionReportEvent", "bad": "data"}
        # Should not raise
        await _handle_event(
            data, notifiers=[], exec_events_enabled=False,
            debounce_buf=None, db_path="/tmp/test.db",
        )


# ── Env var getter tests ────────────────────────────────────────────


class TestEnvVarGetters(unittest.TestCase):
    """Test env var validation and fail-fast behavior."""

    def test_bridge_ws_url_valid(self) -> None:
        self.assertEqual(
            get_bridge_ws_url(), "ws://bridge:5000/ibkr/ws/events",
        )

    def test_bridge_ws_url_missing_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BRIDGE_WS_URL", None)
            with self.assertRaises(SystemExit):
                get_bridge_ws_url()

    def test_bridge_api_token_valid(self) -> None:
        self.assertEqual(get_bridge_api_token(), "test-token")

    def test_bridge_api_token_missing_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BRIDGE_API_TOKEN", None)
            with self.assertRaises(SystemExit):
                get_bridge_api_token()

    def test_listener_enabled_true(self) -> None:
        self.assertTrue(is_listener_enabled())

    def test_listener_enabled_false_empty(self) -> None:
        with patch.dict(os.environ, {"LISTENER_ENABLED": ""}):
            self.assertFalse(is_listener_enabled())

    def test_listener_enabled_false_zero(self) -> None:
        with patch.dict(os.environ, {"LISTENER_ENABLED": "0"}):
            self.assertFalse(is_listener_enabled())

    def test_listener_enabled_false_word(self) -> None:
        with patch.dict(os.environ, {"LISTENER_ENABLED": "false"}):
            self.assertFalse(is_listener_enabled())

    def test_listener_enabled_false_no(self) -> None:
        with patch.dict(os.environ, {"LISTENER_ENABLED": "no"}):
            self.assertFalse(is_listener_enabled())

    def test_exec_events_disabled_by_default(self) -> None:
        self.assertFalse(is_exec_events_enabled())

    def test_exec_events_enabled(self) -> None:
        with patch.dict(os.environ, {"LISTENER_EXEC_EVENTS_ENABLED": "true"}):
            self.assertTrue(is_exec_events_enabled())

    def test_debounce_ms_valid(self) -> None:
        self.assertEqual(get_debounce_ms(), 0)

    def test_debounce_ms_invalid_raises(self) -> None:
        with patch.dict(os.environ, {"LISTENER_EVENT_DEBOUNCE_TIME": "abc"}), \
             self.assertRaises(SystemExit):
            get_debounce_ms()

    def test_debounce_ms_negative_raises(self) -> None:
        with patch.dict(os.environ, {"LISTENER_EVENT_DEBOUNCE_TIME": "-1"}), \
             self.assertRaises(SystemExit):
            get_debounce_ms()


# ── Debounce buffer tests ───────────────────────────────────────────


class TestDebounceBuffer(unittest.IsolatedAsyncioTestCase):
    """Test the debounce buffer batching behavior."""

    @patch("listener._send_and_mark")
    async def test_flush_dispatches_buffered_fills(
        self, mock_send: MagicMock,
    ) -> None:
        from listener import _DebounceBuffer

        buf = _DebounceBuffer(
            debounce_ms=5000, notifiers=[], db_path="/tmp/test.db",
        )
        envelope = _make_envelope(exec_id="A001")
        fill = map_fill(envelope)
        assert fill is not None

        await buf.add(fill)
        self.assertEqual(len(buf._buffer), 1)

        # Flush manually (don't wait for timer)
        await buf.flush()
        mock_send.assert_called_once()
        self.assertEqual(len(buf._buffer), 0)

    @patch("listener._send_and_mark")
    async def test_flush_noop_when_empty(
        self, mock_send: MagicMock,
    ) -> None:
        from listener import _DebounceBuffer

        buf = _DebounceBuffer(
            debounce_ms=5000, notifiers=[], db_path="/tmp/test.db",
        )
        await buf.flush()
        mock_send.assert_not_called()

    @patch("listener._send_and_mark")
    async def test_delayed_flush_fires_after_debounce(
        self, mock_send: MagicMock,
    ) -> None:
        from listener import _DebounceBuffer

        buf = _DebounceBuffer(
            debounce_ms=50, notifiers=[], db_path="/tmp/test.db",
        )
        envelope = _make_envelope(exec_id="B001")
        fill = map_fill(envelope)
        assert fill is not None

        await buf.add(fill)
        # Wait for the debounce timer to fire
        await asyncio.sleep(0.15)
        mock_send.assert_called_once()

    @patch("listener._send_and_mark", side_effect=RuntimeError("webhook down"))
    async def test_flush_restores_fills_on_error(
        self, mock_send: MagicMock,
    ) -> None:
        """Fills are re-added to the buffer when _send_and_mark raises."""
        from listener import _DebounceBuffer

        buf = _DebounceBuffer(
            debounce_ms=5000, notifiers=[], db_path="/tmp/test.db",
        )
        envelope = _make_envelope(exec_id="ERR1")
        fill = map_fill(envelope)
        assert fill is not None

        await buf.add(fill)
        self.assertEqual(len(buf._buffer), 1)

        # flush() catches Exception — should not raise
        await buf.flush()
        mock_send.assert_called_once()
        # Fill must be restored
        self.assertEqual(len(buf._buffer), 1)
        self.assertEqual(buf._buffer[0].execId, "ERR1")

    async def test_flush_restores_fills_on_cancellation(self) -> None:
        """Fills are re-added when flush is cancelled during to_thread."""
        from listener import _DebounceBuffer

        flush_started = asyncio.Event()

        async def slow_to_thread(*args: Any, **kwargs: Any) -> None:
            flush_started.set()
            await asyncio.sleep(10)  # Will be cancelled

        buf = _DebounceBuffer(
            debounce_ms=5000, notifiers=[], db_path="/tmp/test.db",
        )
        envelope = _make_envelope(exec_id="CAN1")
        fill = map_fill(envelope)
        assert fill is not None
        await buf.add(fill)

        with patch("asyncio.to_thread", side_effect=slow_to_thread):
            task = asyncio.create_task(buf.flush())
            await flush_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        # Fill must be restored and flushing flag reset
        self.assertEqual(len(buf._buffer), 1)
        self.assertEqual(buf._buffer[0].execId, "CAN1")
        self.assertFalse(buf._flushing)

    async def test_add_does_not_cancel_in_progress_flush(self) -> None:
        """add() skips cancel when a flush is already in progress."""
        from listener import _DebounceBuffer

        flush_entered = asyncio.Event()
        flush_proceed = asyncio.Event()

        async def gated_to_thread(fn: Any, *args: Any) -> None:
            flush_entered.set()
            await flush_proceed.wait()

        buf = _DebounceBuffer(
            debounce_ms=5000, notifiers=[], db_path="/tmp/test.db",
        )
        envelope1 = _make_envelope(exec_id="F001")
        fill1 = map_fill(envelope1)
        assert fill1 is not None
        await buf.add(fill1)

        with patch("asyncio.to_thread", side_effect=gated_to_thread):
            # Start flush manually (debounce_ms is large, so no auto-fire)
            flush_task = asyncio.create_task(buf.flush())
            await flush_entered.wait()
            self.assertTrue(buf._flushing)

            # add() during flush — should NOT cancel flush_task
            envelope2 = _make_envelope(exec_id="F002")
            fill2 = map_fill(envelope2)
            assert fill2 is not None
            await buf.add(fill2)

            # The flush task must still be running (not cancelled)
            self.assertFalse(flush_task.done())

            # Let flush complete
            flush_proceed.set()
            await flush_task

        # F002 remains in buffer for the next flush
        self.assertIn("F002", [f.execId for f in buf._buffer])
        self.assertFalse(buf._flushing)

    @patch("listener._send_and_mark", side_effect=RuntimeError("boom"))
    async def test_fills_added_during_failed_flush_preserved(
        self, mock_send: MagicMock,
    ) -> None:
        """Fills added while flush is in-flight are not lost on error."""
        from listener import _DebounceBuffer

        buf = _DebounceBuffer(
            debounce_ms=5000, notifiers=[], db_path="/tmp/test.db",
        )
        envelope1 = _make_envelope(exec_id="OLD1")
        fill1 = map_fill(envelope1)
        assert fill1 is not None

        envelope2 = _make_envelope(exec_id="NEW1")
        fill2 = map_fill(envelope2)
        assert fill2 is not None

        await buf.add(fill1)
        # Manually trigger flush (will fail), then add fill2 to simulate
        # a fill arriving while flush was in progress
        await buf.flush()

        # OLD1 was restored to front on error
        self.assertEqual(buf._buffer[0].execId, "OLD1")

        # Now add NEW1 — it should go after the restored fills
        await buf.add(fill2)
        exec_ids = [f.execId for f in buf._buffer]
        self.assertEqual(exec_ids, ["OLD1", "NEW1"])
