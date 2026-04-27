"""Tests for the generic WS listener engine."""

import asyncio
import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import aiohttp

from relay_core import ListenerConfig, OnMessageResult
from relay_core.context import get_relay
from relay_core.dedup import get_processed_ids, init_db, mark_processed_batch
from relay_core.listener_engine import (
    DebounceBuffer,
    _handle_event,
    _prefix_ids,
    _send_and_mark,
    _send_no_mark,
    _strip_prefix,
)
from shared import BuySell, Fill

# ── Module-level FX guard ────────────────────────────────────────────
# _send_and_mark / _send_no_mark call enrich_if_enabled(), which reads
# FX_RATES_ENABLED and caches a process-wide singleton on first use.
# Force FX off for the entire module so a developer with
# FX_RATES_ENABLED=true in their shell cannot make tests env-dependent
# or trigger network / cache I/O.

_ORIG_FX_ENABLED: str | None = None


def setUpModule() -> None:
    from relay_core.fx import _reset_for_tests

    global _ORIG_FX_ENABLED
    _ORIG_FX_ENABLED = os.environ.get("FX_RATES_ENABLED")
    os.environ["FX_RATES_ENABLED"] = "false"
    _reset_for_tests()


def tearDownModule() -> None:
    from relay_core.fx import _reset_for_tests

    if _ORIG_FX_ENABLED is None:
        os.environ.pop("FX_RATES_ENABLED", None)
    else:
        os.environ["FX_RATES_ENABLED"] = _ORIG_FX_ENABLED
    _reset_for_tests()


async def _dummy_connect(session: aiohttp.ClientSession) -> aiohttp.ClientWebSocketResponse:
    """Placeholder connect callback — tests never call it."""
    raise NotImplementedError("test dummy")


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
) -> list[OnMessageResult]:
    """Default no-op on_message for tests that don't need it."""
    return []


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

    @patch("relay_core.listener_engine.mark_processed_batch")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_ids", return_value=set())
    @patch("relay_core.listener_engine._init_dedup_db")
    def test_parse_errors_included_in_payload(
        self,
        mock_init_db: MagicMock,
        mock_get_ids: MagicMock,
        mock_notify: MagicMock,
        mock_mark: MagicMock,
    ) -> None:
        """parse_errors appear in payload.errors alongside fills."""
        mock_conn = MagicMock()
        mock_init_db.return_value = mock_conn

        fill = _make_fill()
        _send_and_mark("ibkr", [fill], "/tmp/test.db", parse_errors=["bad timestamp"])

        mock_notify.assert_called_once()
        payload = mock_notify.call_args[0][1]
        self.assertIn("bad timestamp", payload.errors)

    @patch("relay_core.listener_engine.mark_processed_batch")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_ids", return_value=set())
    @patch("relay_core.listener_engine._init_dedup_db")
    def test_errors_only_triggers_notify_no_mark(
        self,
        mock_init_db: MagicMock,
        mock_get_ids: MagicMock,
        mock_notify: MagicMock,
        mock_mark: MagicMock,
    ) -> None:
        """parse_errors alone (no fills) still call notify; nothing is marked."""
        mock_conn = MagicMock()
        mock_init_db.return_value = mock_conn

        _send_and_mark("ibkr", [], "/tmp/test.db", parse_errors=["unrecognised side"])

        mock_notify.assert_called_once()
        payload = mock_notify.call_args[0][1]
        self.assertEqual(payload.errors, ["unrecognised side"])
        self.assertEqual(payload.data, [])
        mock_mark.assert_not_called()

    @patch("relay_core.listener_engine.mark_processed_batch")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_ids")
    @patch("relay_core.listener_engine._init_dedup_db")
    def test_already_seen_fill_with_errors_still_notifies(
        self,
        mock_init_db: MagicMock,
        mock_get_ids: MagicMock,
        mock_notify: MagicMock,
        mock_mark: MagicMock,
    ) -> None:
        """When a fill is deduped away but parse_errors exist, notify is still called."""
        mock_conn = MagicMock()
        mock_init_db.return_value = mock_conn
        mock_get_ids.return_value = {"ibkr:0001"}

        fill = _make_fill()
        _send_and_mark("ibkr", [fill], "/tmp/test.db", parse_errors=["missing qty"])

        mock_notify.assert_called_once()
        payload = mock_notify.call_args[0][1]
        self.assertEqual(payload.errors, ["missing qty"])
        self.assertEqual(payload.data, [])
        mock_mark.assert_not_called()


# ── _send_and_mark with REAL SQLite (in-memory-style file) ──────────


class TestSendAndMarkRealDb(unittest.TestCase):
    """Exercise the full dedup pipeline against a real SQLite DB."""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmp_dir.name) / "test.db")

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()

    @patch("relay_core.listener_engine.notify")
    def test_new_fill_persisted_in_real_db(self, mock_notify: MagicMock) -> None:
        """End-to-end: new fill triggers notify and is written to the dedup DB."""
        fill = _make_fill(exec_id="REAL_1")
        _send_and_mark("ibkr", [fill], self._db_path)

        mock_notify.assert_called_once()
        conn = init_db(Path(self._db_path))
        try:
            seen = get_processed_ids(conn, {"ibkr:REAL_1"})
            self.assertEqual(seen, {"ibkr:REAL_1"})
        finally:
            conn.close()

    @patch("relay_core.listener_engine.notify")
    def test_same_fill_twice_only_notifies_once(
        self, mock_notify: MagicMock,
    ) -> None:
        """Sending the same fill twice — the second call is deduped against the real DB."""
        fill = _make_fill(exec_id="DUP")
        _send_and_mark("ibkr", [fill], self._db_path)
        _send_and_mark("ibkr", [fill], self._db_path)
        mock_notify.assert_called_once()

    @patch("relay_core.listener_engine.notify")
    def test_mixed_batch_only_new_fills_processed(
        self, mock_notify: MagicMock,
    ) -> None:
        """A batch with seen + new fills: only new ones are notified and marked."""
        # Pre-mark one fill in the real DB
        conn = init_db(Path(self._db_path))
        try:
            mark_processed_batch(conn, ["ibkr:SEEN"])
        finally:
            conn.close()

        seen = _make_fill(exec_id="SEEN")
        new1 = _make_fill(exec_id="NEW1")
        new2 = _make_fill(exec_id="NEW2")
        _send_and_mark("ibkr", [seen, new1, new2], self._db_path)

        mock_notify.assert_called_once()
        payload = mock_notify.call_args[0][1]
        sent_exec_ids = {eid for t in payload.data for eid in t.execIds}
        # SEEN must be excluded; only the two new IDs were dispatched
        self.assertEqual(sent_exec_ids, {"NEW1", "NEW2"})

        # All three are now in the DB (SEEN pre-existing, NEW1+NEW2 newly marked)
        conn = init_db(Path(self._db_path))
        try:
            all_seen = get_processed_ids(
                conn, {"ibkr:SEEN", "ibkr:NEW1", "ibkr:NEW2"},
            )
            self.assertEqual(
                all_seen, {"ibkr:SEEN", "ibkr:NEW1", "ibkr:NEW2"},
            )
        finally:
            conn.close()

    @patch(
        "relay_core.listener_engine.notify",
        side_effect=RuntimeError("notify down"),
    )
    def test_notify_failure_does_not_mark(
        self, mock_notify: MagicMock,
    ) -> None:
        """Mark-after-notify guarantee: if notify raises, the fill must NOT be marked.

        Verified against the real DB to ensure the contract holds end-to-end.
        """
        fill = _make_fill(exec_id="NOTIFY_FAIL")
        with self.assertRaises(RuntimeError):
            _send_and_mark("ibkr", [fill], self._db_path)

        # notify was called (and raised) — confirm the failure happened at notify, not earlier
        mock_notify.assert_called_once()

        conn = init_db(Path(self._db_path))
        try:
            seen = get_processed_ids(conn, {"ibkr:NOTIFY_FAIL"})
            self.assertEqual(seen, set())
        finally:
            conn.close()


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

    @patch("relay_core.listener_engine.notify")
    def test_parse_errors_included_in_payload(self, mock_notify: MagicMock) -> None:
        """parse_errors appear in the payload sent by _send_no_mark."""
        fill = _make_fill()
        _send_no_mark("ibkr", [fill], parse_errors=["missing price field"])

        mock_notify.assert_called_once()
        payload = mock_notify.call_args[0][1]
        self.assertIn("missing price field", payload.errors)

    @patch("relay_core.listener_engine.notify")
    def test_errors_only_triggers_notify(self, mock_notify: MagicMock) -> None:
        """parse_errors alone (no fills) still call notify via _send_no_mark."""
        _send_no_mark("ibkr", [], parse_errors=["unknown asset class"])

        mock_notify.assert_called_once()
        payload = mock_notify.call_args[0][1]
        self.assertEqual(payload.errors, ["unknown asset class"])
        self.assertEqual(payload.data, [])


# ── Notifier-dispatch ordering contract ─────────────────────────────


class TestDispatchOrdering(unittest.TestCase):
    """Trades reach notify() sorted by timestamp ascending.

    Sorted at each call site (not inside aggregate_fills), so verified
    independently for both _send_and_mark and _send_no_mark.
    """

    @staticmethod
    def _fill(exec_id: str, order_id: str, timestamp: str) -> Fill:
        return Fill(
            execId=exec_id,
            orderId=order_id,
            symbol="X",
            assetClass="equity",
            side=BuySell.BUY,
            orderType=None,
            price=100.0,
            volume=1.0,
            cost=100.0,
            fee=0.0,
            timestamp=timestamp,
            source="commissionReportEvent",
            raw={},
        )

    @patch("relay_core.listener_engine.mark_processed_batch")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_ids", return_value=set())
    @patch("relay_core.listener_engine._init_dedup_db")
    def test_send_and_mark_sorts_trades_by_timestamp_ascending(
        self,
        mock_init_db: MagicMock,
        mock_get_ids: MagicMock,
        mock_notify: MagicMock,
        mock_mark: MagicMock,
    ) -> None:
        mock_init_db.return_value = MagicMock()

        f_late = self._fill("L", "O_LATE", "2026-04-22T09:28:31")
        f_early = self._fill("E", "O_EARLY", "2026-03-27T13:44:55")
        f_mid = self._fill("M", "O_MID", "2026-04-06T09:47:31")

        _send_and_mark("ibkr", [f_late, f_early, f_mid], "/tmp/test.db")

        mock_notify.assert_called_once()
        payload = mock_notify.call_args[0][1]
        timestamps = [t.timestamp for t in payload.data]
        self.assertEqual(
            timestamps,
            [
                "2026-03-27T13:44:55",
                "2026-04-06T09:47:31",
                "2026-04-22T09:28:31",
            ],
        )

    @patch("relay_core.listener_engine.notify")
    def test_send_no_mark_sorts_trades_by_timestamp_ascending(
        self, mock_notify: MagicMock,
    ) -> None:
        f_late = self._fill("L", "O_LATE", "2026-04-22T09:28:31")
        f_early = self._fill("E", "O_EARLY", "2026-03-27T13:44:55")
        f_mid = self._fill("M", "O_MID", "2026-04-06T09:47:31")

        _send_no_mark("ibkr", [f_late, f_early, f_mid])

        mock_notify.assert_called_once()
        payload = mock_notify.call_args[0][1]
        timestamps = [t.timestamp for t in payload.data]
        self.assertEqual(
            timestamps,
            [
                "2026-03-27T13:44:55",
                "2026-04-06T09:47:31",
                "2026-04-22T09:28:31",
            ],
        )


# ── _handle_event tests ─────────────────────────────────────────────


class TestHandleEvent(unittest.IsolatedAsyncioTestCase):
    """Test event filtering and dispatch handler plumbing."""

    async def test_event_filter_false_skips(self) -> None:
        """Events rejected by event_filter never reach on_message."""
        called = False

        async def on_msg(
            data: dict[str, Any],
        ) -> list[OnMessageResult]:
            nonlocal called
            called = True
            return []

        config = ListenerConfig(
            connect=_dummy_connect,
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
        ) -> list[OnMessageResult]:
            captured["data"] = data
            return []

        config = ListenerConfig(
            connect=_dummy_connect,
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
        ) -> list[OnMessageResult]:
            return [OnMessageResult(fill=fill, mark=True)]

        config = ListenerConfig(
            connect=_dummy_connect,
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
        ) -> list[OnMessageResult]:
            return [OnMessageResult(fill=fill, mark=False)]

        config = ListenerConfig(
            connect=_dummy_connect,
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
        ) -> list[OnMessageResult]:
            return [OnMessageResult(fill=fill, mark=True)]

        config = ListenerConfig(
            connect=_dummy_connect,
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
        ) -> list[OnMessageResult]:
            return []

        config = ListenerConfig(
            connect=_dummy_connect,
            on_message=on_msg, event_filter=lambda _: True,
        )
        # Should not raise
        _set_listener(config)
        await _handle_event(
            "ibkr", {"type": "x"},
            debounce_buf=None, db_path="/tmp/test.db",
        )

    @patch("relay_core.listener_engine._send_no_mark")
    @patch("relay_core.listener_engine._send_and_mark")
    async def test_multi_result_splits_mark_and_no_mark(
        self, mock_send_mark: MagicMock, mock_send_no_mark: MagicMock,
    ) -> None:
        """Multiple results are split: mark=True -> _send_and_mark, mark=False -> _send_no_mark."""
        fill_a = _make_fill(exec_id="A", symbol="AAPL")
        fill_b = _make_fill(exec_id="B", symbol="MSFT")
        fill_c = _make_fill(exec_id="C", symbol="GOOG")

        async def on_msg(
            data: dict[str, Any],
        ) -> list[OnMessageResult]:
            return [
                OnMessageResult(fill=fill_a, mark=True),
                OnMessageResult(fill=fill_b, mark=False),
                OnMessageResult(fill=None),          # skipped
                OnMessageResult(fill=fill_c, mark=True),
            ]

        config = ListenerConfig(
            connect=_dummy_connect,
            on_message=on_msg, event_filter=lambda _: True,
        )
        _set_listener(config)
        await _handle_event(
            "ibkr", {"type": "x"},
            debounce_buf=None, db_path="/tmp/test.db",
        )

        # mark=True fills dispatched together via _send_and_mark
        mock_send_mark.assert_called_once()
        mark_fills = mock_send_mark.call_args[0][1]
        self.assertEqual(len(mark_fills), 2)
        self.assertEqual(mark_fills[0].execId, "A")
        self.assertEqual(mark_fills[1].execId, "C")

        # mark=False fill dispatched via _send_no_mark
        mock_send_no_mark.assert_called_once()
        no_mark_fills = mock_send_no_mark.call_args[0][1]
        self.assertEqual(len(no_mark_fills), 1)
        self.assertEqual(no_mark_fills[0].execId, "B")

    @patch("relay_core.listener_engine._send_no_mark")
    @patch("relay_core.listener_engine._send_and_mark")
    async def test_multi_result_mark_fills_use_debounce_buffer(
        self, mock_send_mark: MagicMock, mock_send_no_mark: MagicMock,
    ) -> None:
        """With debounce buffer, mark=True fills go to buffer; mark=False still dispatch directly."""
        fill_a = _make_fill(exec_id="A", symbol="AAPL")
        fill_b = _make_fill(exec_id="B", symbol="MSFT")
        fill_c = _make_fill(exec_id="C", symbol="GOOG")

        async def on_msg(
            data: dict[str, Any],
        ) -> list[OnMessageResult]:
            return [
                OnMessageResult(fill=fill_a, mark=True),
                OnMessageResult(fill=fill_b, mark=False),
                OnMessageResult(fill=fill_c, mark=True),
            ]

        config = ListenerConfig(
            connect=_dummy_connect,
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

        # mark=True fills buffered, not dispatched directly
        self.assertEqual(len(buf._buffer), 2)
        self.assertEqual(buf._buffer[0].execId, "A")
        self.assertEqual(buf._buffer[1].execId, "C")
        mock_send_mark.assert_not_called()

        # mark=False fill still dispatched via _send_no_mark
        mock_send_no_mark.assert_called_once()
        no_mark_fills = mock_send_no_mark.call_args[0][1]
        self.assertEqual(len(no_mark_fills), 1)
        self.assertEqual(no_mark_fills[0].execId, "B")

    async def test_non_dict_string_skipped(self) -> None:
        """A JSON string (not a dict) is silently skipped."""
        called = False

        async def on_msg(
            data: dict[str, Any],
        ) -> list[OnMessageResult]:
            nonlocal called
            called = True
            return []

        config = ListenerConfig(
            connect=_dummy_connect,
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
        ) -> list[OnMessageResult]:
            nonlocal called
            called = True
            return []

        config = ListenerConfig(
            connect=_dummy_connect,
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
        ) -> list[OnMessageResult]:
            nonlocal called
            called = True
            return []

        config = ListenerConfig(
            connect=_dummy_connect,
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
        ) -> list[OnMessageResult]:
            nonlocal called
            called = True
            return []

        config = ListenerConfig(
            connect=_dummy_connect,
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
        ) -> list[OnMessageResult]:
            nonlocal called
            called = True
            return []

        config = ListenerConfig(
            connect=_dummy_connect,
            on_message=on_msg, event_filter=lambda _: True,
        )
        _set_listener(config)
        await _handle_event(
            "ibkr", {"type": "test"},
            debounce_buf=None, db_path="/tmp/test.db",
        )
        self.assertTrue(called)

    @patch(
        "relay_core.listener_engine._send_and_mark",
        side_effect=RuntimeError("boom"),
    )
    async def test_send_and_mark_failure_is_swallowed(
        self, mock_send: MagicMock,
    ) -> None:
        """Exceptions from _send_and_mark must be caught — never break the event loop."""
        fill = _make_fill()

        async def on_msg(
            data: dict[str, Any],
        ) -> list[OnMessageResult]:
            return [OnMessageResult(fill=fill, mark=True)]

        config = ListenerConfig(
            connect=_dummy_connect,
            on_message=on_msg, event_filter=lambda _: True,
        )
        _set_listener(config)
        # Must not propagate
        await _handle_event(
            "ibkr", {"type": "x"},
            debounce_buf=None, db_path="/tmp/test.db",
        )
        mock_send.assert_called_once()

    @patch(
        "relay_core.listener_engine._send_no_mark",
        side_effect=RuntimeError("boom"),
    )
    async def test_send_no_mark_failure_is_swallowed(
        self, mock_send: MagicMock,
    ) -> None:
        """Exceptions from _send_no_mark must be caught — never break the event loop."""
        fill = _make_fill()

        async def on_msg(
            data: dict[str, Any],
        ) -> list[OnMessageResult]:
            return [OnMessageResult(fill=fill, mark=False)]

        config = ListenerConfig(
            connect=_dummy_connect,
            on_message=on_msg, event_filter=lambda _: True,
        )
        _set_listener(config)
        await _handle_event(
            "ibkr", {"type": "x"},
            debounce_buf=None, db_path="/tmp/test.db",
        )
        mock_send.assert_called_once()

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_error_result_forwarded_to_send_and_mark_with_fill(
        self, mock_send: MagicMock,
    ) -> None:
        """error + mark fill, no debounce → parse_errors forwarded to _send_and_mark."""
        fill = _make_fill()

        async def on_msg(
            data: dict[str, Any],
        ) -> list[OnMessageResult]:
            return [
                OnMessageResult(fill=fill, mark=True),
                OnMessageResult(fill=None, error="bad timestamp"),
            ]

        config = ListenerConfig(
            connect=_dummy_connect,
            on_message=on_msg, event_filter=lambda _: True,
        )
        _set_listener(config)
        await _handle_event(
            "ibkr", {"type": "x"},
            debounce_buf=None, db_path="/tmp/test.db",
        )

        mock_send.assert_called_once()
        call_args = mock_send.call_args[0]
        # positional: relay_name, fills, db_path, parse_errors
        self.assertEqual(call_args[3], ["bad timestamp"])

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_error_only_result_not_dispatched_without_debounce(
        self, mock_send: MagicMock,
    ) -> None:
        """error-only results with no fills and no debounce buffer are silently dropped."""
        async def on_msg(
            data: dict[str, Any],
        ) -> list[OnMessageResult]:
            return [OnMessageResult(fill=None, error="unrecognised side")]

        config = ListenerConfig(
            connect=_dummy_connect,
            on_message=on_msg, event_filter=lambda _: True,
        )
        _set_listener(config)
        await _handle_event(
            "ibkr", {"type": "x"},
            debounce_buf=None, db_path="/tmp/test.db",
        )

        mock_send.assert_not_called()

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_error_result_accumulated_in_debounce_buf_with_fill(
        self, mock_send: MagicMock,
    ) -> None:
        """error + mark fill + debounce → fill buffered, error accumulated via extend_errors."""
        fill = _make_fill()

        async def on_msg(
            data: dict[str, Any],
        ) -> list[OnMessageResult]:
            return [
                OnMessageResult(fill=fill, mark=True),
                OnMessageResult(fill=None, error="bad timestamp"),
            ]

        config = ListenerConfig(
            connect=_dummy_connect,
            on_message=on_msg, event_filter=lambda _: True,
        )
        buf = DebounceBuffer(relay_name="ibkr", debounce_ms=5000, db_path="/tmp/test.db")
        _set_listener(config)
        await _handle_event(
            "ibkr", {"type": "x"},
            debounce_buf=buf, db_path="/tmp/test.db",
        )

        self.assertEqual(len(buf._buffer), 1)
        self.assertEqual(buf._buffer[0].execId, fill.execId)
        self.assertEqual(buf._parse_errors, ["bad timestamp"])
        mock_send.assert_not_called()

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_error_only_result_not_accumulated_in_debounce_buf(
        self, mock_send: MagicMock,
    ) -> None:
        """error-only results (no fills) are not forwarded to the debounce buffer."""
        async def on_msg(
            data: dict[str, Any],
        ) -> list[OnMessageResult]:
            return [OnMessageResult(fill=None, error="unrecognised side")]

        config = ListenerConfig(
            connect=_dummy_connect,
            on_message=on_msg, event_filter=lambda _: True,
        )
        buf = DebounceBuffer(relay_name="ibkr", debounce_ms=5000, db_path="/tmp/test.db")
        _set_listener(config)
        await _handle_event(
            "ibkr", {"type": "x"},
            debounce_buf=buf, db_path="/tmp/test.db",
        )

        self.assertEqual(len(buf._buffer), 0)
        self.assertEqual(buf._parse_errors, [])
        mock_send.assert_not_called()

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_fill_none_without_error_not_accumulated(
        self, mock_send: MagicMock,
    ) -> None:
        """fill=None with no error field produces no error entry — result is fully silent."""
        async def on_msg(
            data: dict[str, Any],
        ) -> list[OnMessageResult]:
            return [OnMessageResult(fill=None)]  # no error set

        config = ListenerConfig(
            connect=_dummy_connect,
            on_message=on_msg, event_filter=lambda _: True,
        )
        buf = DebounceBuffer(relay_name="ibkr", debounce_ms=5000, db_path="/tmp/test.db")
        _set_listener(config)
        await _handle_event(
            "ibkr", {"type": "x"},
            debounce_buf=buf, db_path="/tmp/test.db",
        )

        self.assertEqual(buf._parse_errors, [])
        mock_send.assert_not_called()


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

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_timer_resets_on_subsequent_add(
        self, mock_send: MagicMock,
    ) -> None:
        """Adding a fill before the debounce window expires cancels the pending
        flush task and starts a new one — verified via task identity so the
        assertion does not depend on wall-clock sleep precision.
        """
        buf = DebounceBuffer(
            relay_name="ibkr", debounce_ms=10_000,
            db_path="/tmp/test.db",
        )
        fill1 = _make_fill(exec_id="A1")
        fill2 = _make_fill(exec_id="A2")

        await buf.add(fill1)
        first_task = buf._flush_task
        assert first_task is not None

        await buf.add(fill2)
        second_task = buf._flush_task
        assert second_task is not None

        # The second add must have replaced and cancelled the first task.
        self.assertIsNot(first_task, second_task)
        with self.assertRaises(asyncio.CancelledError):
            await first_task
        mock_send.assert_not_called()

        # Cancel the still-sleeping second task, then flush manually to verify
        # both buffered fills dispatch in a single batch.
        second_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second_task

        await buf.flush()
        mock_send.assert_called_once()
        fills_dispatched = mock_send.call_args[0][1]
        self.assertEqual(
            [f.execId for f in fills_dispatched], ["A1", "A2"],
        )

    async def test_add_during_flush_preserves_new_fill(self) -> None:
        """A fill added while a flush is in progress is preserved for the next flush.

        The in-progress flush has already cleared the buffer and snapshotted
        its fills, so the newly-added fill must NOT be lost: it should sit in
        the buffer waiting for its own debounce cycle.
        """
        flush_started = asyncio.Event()
        flush_can_complete = asyncio.Event()

        async def slow_to_thread(*args: Any, **kwargs: Any) -> None:
            flush_started.set()
            await flush_can_complete.wait()

        buf = DebounceBuffer(
            relay_name="ibkr", debounce_ms=5000,
            db_path="/tmp/test.db",
        )
        first = _make_fill(exec_id="FIRST")
        second = _make_fill(exec_id="SECOND")
        await buf.add(first)

        with patch("asyncio.to_thread", side_effect=slow_to_thread):
            flush_task = asyncio.create_task(buf.flush())
            await flush_started.wait()

            # Buffer has been snapshotted and cleared; flush is in-flight
            self.assertEqual(len(buf._buffer), 0)
            self.assertTrue(buf._flushing)

            # Add a new fill while the flush is mid-flight
            await buf.add(second)
            self.assertEqual(len(buf._buffer), 1)
            self.assertEqual(buf._buffer[0].execId, "SECOND")

            flush_can_complete.set()
            await flush_task

        # FIRST was dispatched; SECOND remains buffered for its own cycle
        self.assertEqual(len(buf._buffer), 1)
        self.assertEqual(buf._buffer[0].execId, "SECOND")
        self.assertFalse(buf._flushing)

        # Cleanup the pending _delayed_flush task scheduled by the second add()
        if buf._flush_task is not None and not buf._flush_task.done():
            buf._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await buf._flush_task

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_extend_errors_flushed_with_fills(
        self, mock_send: MagicMock,
    ) -> None:
        """Errors accumulated via extend_errors are passed to _send_and_mark on flush."""
        buf = DebounceBuffer(relay_name="ibkr", debounce_ms=5000, db_path="/tmp/test.db")
        fill = _make_fill(exec_id="E001")
        await buf.add(fill)
        buf.extend_errors(["bad timestamp"])

        await buf.flush()

        mock_send.assert_called_once()
        call_args = mock_send.call_args[0]
        self.assertEqual(call_args[3], ["bad timestamp"])
        self.assertEqual(buf._parse_errors, [])

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_flush_with_errors_only(
        self, mock_send: MagicMock,
    ) -> None:
        """extend_errors without any fill still triggers _send_and_mark with empty fills."""
        buf = DebounceBuffer(relay_name="ibkr", debounce_ms=5000, db_path="/tmp/test.db")
        buf.extend_errors(["missing field"])

        await buf.flush()

        mock_send.assert_called_once()
        call_args = mock_send.call_args[0]
        self.assertEqual(call_args[1], [])  # empty fills
        self.assertEqual(call_args[3], ["missing field"])
        self.assertEqual(buf._parse_errors, [])

    @patch(
        "relay_core.listener_engine._send_and_mark",
        side_effect=RuntimeError("webhook down"),
    )
    async def test_flush_restores_errors_on_failure(
        self, mock_send: MagicMock,
    ) -> None:
        """Errors are restored to _parse_errors when _send_and_mark fails."""
        buf = DebounceBuffer(relay_name="ibkr", debounce_ms=5000, db_path="/tmp/test.db")
        fill = _make_fill(exec_id="ERR2")
        await buf.add(fill)
        buf.extend_errors(["bad timestamp"])

        await buf.flush()

        self.assertEqual(len(buf._buffer), 1)
        self.assertEqual(buf._buffer[0].execId, "ERR2")
        self.assertEqual(buf._parse_errors, ["bad timestamp"])

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_multiple_extend_errors_accumulate(
        self, mock_send: MagicMock,
    ) -> None:
        """Multiple extend_errors calls accumulate before a single flush."""
        buf = DebounceBuffer(relay_name="ibkr", debounce_ms=5000, db_path="/tmp/test.db")
        fill = _make_fill(exec_id="E002")
        await buf.add(fill)
        buf.extend_errors(["error one"])
        buf.extend_errors(["error two", "error three"])

        await buf.flush()

        mock_send.assert_called_once()
        call_args = mock_send.call_args[0]
        self.assertEqual(call_args[3], ["error one", "error two", "error three"])
