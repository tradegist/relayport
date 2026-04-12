"""Tests for the generic WS listener engine."""

import asyncio
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from relay_core import ListenerConfig, OnMessageResult
from relay_core.context import get_relay
from relay_core.listener_engine import (
    DebounceBuffer,
    _handle_event,
    _prefix_ids,
    _send_and_mark,
    _send_no_mark,
    _strip_prefix,
)
from shared import BuySell, Fill


def _set_listener(config: ListenerConfig) -> None:
    """Set the listener config on the test relay in the context."""
    relay = get_relay("ibkr")
    relay.listener_config = config

# ── Test fill factory ────────────────────────────────────────────────

def _make_fill(
    exec_id: str = "0001",
    symbol: str = "AAPL",
    side: BuySell = BuySell.BUY,
    price: float = 150.25,
    volume: float = 100.0,
    fee: float = 1.05,
) -> Fill:
    return Fill(
        execId=exec_id,
        orderId="12345",
        symbol=symbol,
        assetClass="equity",
        side=side,
        orderType=None,
        price=price,
        volume=volume,
        cost=price * volume,
        fee=fee,
        timestamp="20260411-10:30:00",
        source="commissionReportEvent",
        raw={},
    )


async def _noop_on_message(
    data: dict[str, Any],
) -> OnMessageResult:
    """Default no-op on_message for tests that don't need it."""
    return OnMessageResult()


# ── Namespace helper tests ───────────────────────────────────────────


class TestNamespaceHelpers(unittest.TestCase):
    """Test relay-prefixed ID generation and stripping."""

    def test_prefix_ids(self) -> None:
        fills = [_make_fill(exec_id="A"), _make_fill(exec_id="B")]
        result = _prefix_ids("ibkr", fills)
        self.assertEqual(result, {"ibkr:A", "ibkr:B"})

    def test_strip_prefix(self) -> None:
        prefixed = {"ibkr:A", "ibkr:B"}
        result = _strip_prefix("ibkr", prefixed)
        self.assertEqual(result, {"A", "B"})

    def test_prefix_empty(self) -> None:
        result = _prefix_ids("ibkr", [])
        self.assertEqual(result, set())

    def test_strip_empty(self) -> None:
        result = _strip_prefix("ibkr", set())
        self.assertEqual(result, set())


# ── _send_and_mark tests ────────────────────────────────────────────


class TestSendAndMark(unittest.TestCase):
    """Test the dedup + aggregate + notify + mark pipeline."""

    @patch("relay_core.listener_engine.mark_processed_batch")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_ids", return_value=set())
    @patch("relay_core.listener_engine._init_dedup_db")
    def test_new_fill_dispatched_and_marked(
        self,
        mock_init_db: MagicMock,
        mock_get_ids: MagicMock,
        mock_notify: MagicMock,
        mock_mark: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_init_db.return_value = mock_conn

        fill = _make_fill()
        _send_and_mark("ibkr", [fill], "/tmp/test.db")

        mock_notify.assert_called_once()
        mock_mark.assert_called_once()
        # Verify relay-prefixed exec IDs
        mark_args = mock_mark.call_args[0]
        self.assertEqual(mark_args[0], mock_conn)
        self.assertEqual(mark_args[1], ["ibkr:0001"])
        mock_conn.close.assert_called_once()

    @patch("relay_core.listener_engine.mark_processed_batch")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_ids", return_value=set())
    @patch("relay_core.listener_engine._init_dedup_db")
    def test_dedup_checks_prefixed_ids(
        self,
        mock_init_db: MagicMock,
        mock_get_ids: MagicMock,
        mock_notify: MagicMock,
        mock_mark: MagicMock,
    ) -> None:
        """get_processed_ids is called with relay-prefixed candidate IDs."""
        mock_conn = MagicMock()
        mock_init_db.return_value = mock_conn

        fill = _make_fill(exec_id="X1")
        _send_and_mark("ibkr", [fill], "/tmp/test.db")

        get_ids_args = mock_get_ids.call_args[0]
        self.assertEqual(get_ids_args[1], {"ibkr:X1"})

    @patch("relay_core.listener_engine.mark_processed_batch")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_ids")
    @patch("relay_core.listener_engine._init_dedup_db")
    def test_already_seen_fill_skipped(
        self,
        mock_init_db: MagicMock,
        mock_get_ids: MagicMock,
        mock_notify: MagicMock,
        mock_mark: MagicMock,
    ) -> None:
        mock_conn = MagicMock()
        mock_init_db.return_value = mock_conn
        mock_get_ids.return_value = {"ibkr:0001"}

        fill = _make_fill()
        _send_and_mark("ibkr", [fill], "/tmp/test.db")

        mock_notify.assert_not_called()
        mock_mark.assert_not_called()
        mock_conn.close.assert_called_once()

    @patch("relay_core.listener_engine.mark_processed_batch")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_ids", return_value=set())
    @patch("relay_core.listener_engine._init_dedup_db")
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

        fill = _make_fill()
        with self.assertRaises(RuntimeError):
            _send_and_mark("ibkr", [fill], "/tmp/test.db")

        mock_conn.close.assert_called_once()


# ── _send_no_mark tests ─────────────────────────────────────────────


class TestSendNoMark(unittest.TestCase):
    """Test fire-and-forget dispatch for exec events."""

    @patch("relay_core.listener_engine.notify")
    def test_dispatches_without_marking(self, mock_notify: MagicMock) -> None:
        fill = _make_fill()
        _send_no_mark("ibkr", [fill])

        mock_notify.assert_called_once()
        payload = mock_notify.call_args[0][1]
        self.assertEqual(len(payload.data), 1)
        self.assertEqual(payload.data[0].symbol, "AAPL")
        self.assertEqual(payload.relay, "ibkr")


# ── _handle_event tests ─────────────────────────────────────────────


class TestHandleEvent(unittest.IsolatedAsyncioTestCase):
    """Test event filtering and dispatch handler plumbing."""

    async def test_event_filter_false_skips(self) -> None:
        """Events rejected by event_filter never reach on_message."""
        called = False

        async def on_msg(
            data: dict[str, Any],
        ) -> OnMessageResult:
            nonlocal called
            called = True
            return OnMessageResult()

        config = ListenerConfig(
            ws_url="ws://localhost/ws", api_token="t",
            on_message=on_msg, event_filter=lambda _: False,
        )
        _set_listener(config)
        await _handle_event(
            "ibkr", {"type": "x"},
            debounce_buf=None, db_path="/tmp/test.db",
        )
        self.assertFalse(called)

    async def test_on_message_receives_data(self) -> None:
        """on_message is called with the raw data."""
        captured: dict[str, Any] = {}

        async def on_msg(
            data: dict[str, Any],
        ) -> OnMessageResult:
            captured["data"] = data
            return OnMessageResult()

        config = ListenerConfig(
            ws_url="ws://localhost/ws", api_token="t",
            on_message=on_msg, event_filter=lambda _: True,
        )
        data: dict[str, Any] = {"type": "test", "seq": 1}
        _set_listener(config)
        await _handle_event(
            "ibkr", data,
            debounce_buf=None, db_path="/tmp/test.db",
        )
        self.assertEqual(captured["data"], data)

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_mark_true_dispatches_send_and_mark(
        self, mock_send: MagicMock,
    ) -> None:
        """Returning mark=True triggers the dedup pipeline."""
        fill = _make_fill()

        async def on_msg(
            data: dict[str, Any],
        ) -> OnMessageResult:
            return OnMessageResult(fill=fill, mark=True)

        config = ListenerConfig(
            ws_url="ws://localhost/ws", api_token="t",
            on_message=on_msg, event_filter=lambda _: True,
        )
        _set_listener(config)
        await _handle_event(
            "ibkr", {"type": "x"},
            debounce_buf=None, db_path="/tmp/test.db",
        )
        mock_send.assert_called_once()
        call_args = mock_send.call_args[0]
        self.assertEqual(call_args[0], "ibkr")
        self.assertEqual(len(call_args[1]), 1)

    @patch("relay_core.listener_engine._send_no_mark")
    async def test_mark_false_dispatches_send_no_mark(
        self, mock_send: MagicMock,
    ) -> None:
        """Returning mark=False triggers fire-and-forget dispatch."""
        fill = _make_fill()

        async def on_msg(
            data: dict[str, Any],
        ) -> OnMessageResult:
            return OnMessageResult(fill=fill, mark=False)

        config = ListenerConfig(
            ws_url="ws://localhost/ws", api_token="t",
            on_message=on_msg, event_filter=lambda _: True,
        )
        _set_listener(config)
        await _handle_event(
            "ibkr", {"type": "x"},
            debounce_buf=None, db_path="/tmp/test.db",
        )
        mock_send.assert_called_once()

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_mark_true_uses_debounce_buffer(
        self, mock_send: MagicMock,
    ) -> None:
        """mark=True routes through debounce buffer when present."""
        fill = _make_fill()

        async def on_msg(
            data: dict[str, Any],
        ) -> OnMessageResult:
            return OnMessageResult(fill=fill, mark=True)

        config = ListenerConfig(
            ws_url="ws://localhost/ws", api_token="t",
            on_message=on_msg, event_filter=lambda _: True,
        )
        buf = DebounceBuffer(
            relay_name="ibkr", debounce_ms=5000,
            db_path="/tmp/test.db",
        )
        _set_listener(config)
        await _handle_event(
            "ibkr", {"type": "x"},
            debounce_buf=buf, db_path="/tmp/test.db",
        )
        # Fill should be in buffer, not dispatched directly
        self.assertEqual(len(buf._buffer), 1)
        mock_send.assert_not_called()

    async def test_fill_none_skips_dispatch(self) -> None:
        """If on_message returns fill=None, nothing is dispatched."""
        async def on_msg(
            data: dict[str, Any],
        ) -> OnMessageResult:
            return OnMessageResult()

        config = ListenerConfig(
            ws_url="ws://localhost/ws", api_token="t",
            on_message=on_msg, event_filter=lambda _: True,
        )
        # Should not raise
        _set_listener(config)
        await _handle_event(
            "ibkr", {"type": "x"},
            debounce_buf=None, db_path="/tmp/test.db",
        )

    async def test_non_dict_string_skipped(self) -> None:
        """A JSON string (not a dict) is silently skipped."""
        called = False

        async def on_msg(
            data: dict[str, Any],
        ) -> OnMessageResult:
            nonlocal called
            called = True
            return OnMessageResult()

        config = ListenerConfig(
            ws_url="ws://localhost/ws", api_token="t",
            on_message=on_msg, event_filter=lambda _: True,
        )
        _set_listener(config)
        await _handle_event(
            "ibkr", "just a string",
            debounce_buf=None, db_path="/tmp/test.db",
        )
        self.assertFalse(called)

    async def test_non_dict_list_skipped(self) -> None:
        """A JSON array (not a dict) is silently skipped."""
        called = False

        async def on_msg(
            data: dict[str, Any],
        ) -> OnMessageResult:
            nonlocal called
            called = True
            return OnMessageResult()

        config = ListenerConfig(
            ws_url="ws://localhost/ws", api_token="t",
            on_message=on_msg, event_filter=lambda _: True,
        )
        _set_listener(config)
        await _handle_event(
            "ibkr", [1, 2, 3],
            debounce_buf=None, db_path="/tmp/test.db",
        )
        self.assertFalse(called)

    async def test_non_dict_int_skipped(self) -> None:
        """A JSON integer (not a dict) is silently skipped."""
        called = False

        async def on_msg(
            data: dict[str, Any],
        ) -> OnMessageResult:
            nonlocal called
            called = True
            return OnMessageResult()

        config = ListenerConfig(
            ws_url="ws://localhost/ws", api_token="t",
            on_message=on_msg, event_filter=lambda _: True,
        )
        _set_listener(config)
        await _handle_event(
            "ibkr", 42,
            debounce_buf=None, db_path="/tmp/test.db",
        )
        self.assertFalse(called)

    async def test_non_dict_none_skipped(self) -> None:
        """A JSON null (not a dict) is silently skipped."""
        called = False

        async def on_msg(
            data: dict[str, Any],
        ) -> OnMessageResult:
            nonlocal called
            called = True
            return OnMessageResult()

        config = ListenerConfig(
            ws_url="ws://localhost/ws", api_token="t",
            on_message=on_msg, event_filter=lambda _: True,
        )
        _set_listener(config)
        await _handle_event(
            "ibkr", None,
            debounce_buf=None, db_path="/tmp/test.db",
        )
        self.assertFalse(called)

    async def test_dict_still_processed(self) -> None:
        """A proper dict still passes through to event_filter + on_message."""
        called = False

        async def on_msg(
            data: dict[str, Any],
        ) -> OnMessageResult:
            nonlocal called
            called = True
            return OnMessageResult()

        config = ListenerConfig(
            ws_url="ws://localhost/ws", api_token="t",
            on_message=on_msg, event_filter=lambda _: True,
        )
        _set_listener(config)
        await _handle_event(
            "ibkr", {"type": "test"},
            debounce_buf=None, db_path="/tmp/test.db",
        )
        self.assertTrue(called)


# ── DebounceBuffer tests ────────────────────────────────────────────


class TestDebounceBuffer(unittest.IsolatedAsyncioTestCase):
    """Test the debounce buffer batching behavior."""

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_flush_dispatches_buffered_fills(
        self, mock_send: MagicMock,
    ) -> None:
        buf = DebounceBuffer(
            relay_name="ibkr", debounce_ms=5000,
            db_path="/tmp/test.db",
        )
        fill = _make_fill(exec_id="A001")
        await buf.add(fill)
        self.assertEqual(len(buf._buffer), 1)

        await buf.flush()
        mock_send.assert_called_once()
        # Verify relay_name passed to _send_and_mark
        call_args = mock_send.call_args[0]
        self.assertEqual(call_args[0], "ibkr")
        self.assertEqual(len(buf._buffer), 0)

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_flush_noop_when_empty(
        self, mock_send: MagicMock,
    ) -> None:
        buf = DebounceBuffer(
            relay_name="ibkr", debounce_ms=5000,
            db_path="/tmp/test.db",
        )
        await buf.flush()
        mock_send.assert_not_called()

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_delayed_flush_fires_after_debounce(
        self, mock_send: MagicMock,
    ) -> None:
        buf = DebounceBuffer(
            relay_name="ibkr", debounce_ms=50,
            db_path="/tmp/test.db",
        )
        fill = _make_fill(exec_id="B001")
        await buf.add(fill)
        await asyncio.sleep(0.15)
        mock_send.assert_called_once()

    @patch(
        "relay_core.listener_engine._send_and_mark",
        side_effect=RuntimeError("webhook down"),
    )
    async def test_flush_restores_fills_on_error(
        self, mock_send: MagicMock,
    ) -> None:
        """Fills are re-added to the buffer when _send_and_mark raises."""
        buf = DebounceBuffer(
            relay_name="ibkr", debounce_ms=5000,
            db_path="/tmp/test.db",
        )
        fill = _make_fill(exec_id="ERR1")
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
        flush_started = asyncio.Event()

        async def slow_to_thread(*args: Any, **kwargs: Any) -> None:
            flush_started.set()
            await asyncio.sleep(10)  # Will be cancelled

        buf = DebounceBuffer(
            relay_name="ibkr", debounce_ms=5000,
            db_path="/tmp/test.db",
        )
        fill = _make_fill(exec_id="CAN1")
        await buf.add(fill)

        with patch("asyncio.to_thread", side_effect=slow_to_thread):
            task = asyncio.create_task(buf.flush())
            await flush_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(len(buf._buffer), 1)
        self.assertEqual(buf._buffer[0].execId, "CAN1")
