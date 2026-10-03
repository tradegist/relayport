"""Tests for the generic WS listener engine."""

import asyncio
import contextlib
import json
import os
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import aiohttp
import httpx

from relay_core import BrokerRelay, ListenerConfig, OnMessageResult
from relay_core.context import _reset, get_relay, init_relays
from relay_core.dedup import (
    get_processed_ids,
    get_processed_rows,
    init_db,
    mark_book_trade_keys,
    mark_processed_batch,
)
from relay_core.inflight import INFLIGHT_ORDERS
from relay_core.listener_engine import (
    DebounceBuffer,
    FatalListenerError,
    _handle_event,
    _listen,
    _merge_fills,
    _prefix_ids,
    _send_and_mark,
    _send_no_mark,
    _split_stale,
    get_max_fill_age_s,
    get_retry_delays_s,
)
from relay_core.notifier import NotificationError, is_transient_failure
from shared import BuySell, Fill, to_epoch

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
    order_id: str = "12345",
    timestamp: str = "20260411-10:30:00",
) -> Fill:
    return Fill(
        execId=exec_id,
        orderId=order_id,
        symbol=symbol,
        assetClass="equity",
        side=side,
        orderType=None,
        price=price,
        volume=volume,
        cost=price * volume,
        fee=fee,
        timestamp=timestamp,
        source="commissionReportEvent",
        raw={},
    )


async def _noop_on_message(
    data: dict[str, Any],
) -> list[OnMessageResult]:
    """Default no-op on_message for tests that don't need it."""
    return []


def _immediate_buffer() -> DebounceBuffer:
    """Buffer with debouncing disabled — every fill is flushed on add()."""
    return DebounceBuffer(relay_name="ibkr", debounce_ms=0, db_path="/tmp/test.db")


# ── Namespace helper tests ───────────────────────────────────────────


class TestNamespaceHelpers(unittest.TestCase):
    """Test relay-prefixed ID generation."""

    def test_prefix_ids(self) -> None:
        fills = [_make_fill(exec_id="A"), _make_fill(exec_id="B")]
        result = _prefix_ids("ibkr", fills)
        self.assertEqual(result, {"ibkr:A", "ibkr:B"})

    def test_prefix_empty(self) -> None:
        result = _prefix_ids("ibkr", [])
        self.assertEqual(result, set())


# ── In-flight registration tests ─────────────────────────────────────


class TestInFlightRegistration(unittest.TestCase):
    """_send_and_mark must hold its orderIds in the in-flight registry for
    exactly the notify+mark span, releasing on success AND failure — the
    poller defers registered orders to avoid duplicate webhooks."""

    def setUp(self) -> None:
        INFLIGHT_ORDERS._counts.clear()
        self.addCleanup(INFLIGHT_ORDERS._counts.clear)

    @patch("relay_core.listener_engine.mark_processed_batch_with_orders")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_rows", return_value={})
    @patch("relay_core.listener_engine._init_dedup_db")
    def test_order_registered_during_notify(
        self,
        mock_init_db: MagicMock,
        mock_get_ids: MagicMock,
        mock_notify: MagicMock,
        mock_mark: MagicMock,
    ) -> None:
        mock_init_db.return_value = MagicMock()
        seen_during_notify: set[str] = set()

        def capture(*args: Any, **kwargs: Any) -> None:
            seen_during_notify.update(
                INFLIGHT_ORDERS.intersect("ibkr", {"12345"}),
            )

        mock_notify.side_effect = capture

        _send_and_mark("ibkr", [_make_fill(order_id="12345")], "/tmp/test.db")

        self.assertEqual(seen_during_notify, {"12345"})
        # Released after the pipeline completes.
        self.assertEqual(INFLIGHT_ORDERS.intersect("ibkr", {"12345"}), set())

    @patch("relay_core.listener_engine.mark_processed_batch_with_orders")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_rows", return_value={})
    @patch("relay_core.listener_engine._init_dedup_db")
    def test_order_released_when_notify_fails(
        self,
        mock_init_db: MagicMock,
        mock_get_ids: MagicMock,
        mock_notify: MagicMock,
        mock_mark: MagicMock,
    ) -> None:
        mock_init_db.return_value = MagicMock()
        mock_notify.side_effect = RuntimeError("all notifiers failed")

        with self.assertRaises(RuntimeError):
            _send_and_mark("ibkr", [_make_fill(order_id="12345")], "/tmp/test.db")

        self.assertEqual(INFLIGHT_ORDERS.intersect("ibkr", {"12345"}), set())
        mock_mark.assert_not_called()


# ── _send_and_mark tests ────────────────────────────────────────────


class TestSendAndMark(unittest.TestCase):
    """Test the dedup + aggregate + notify + mark pipeline."""

    @patch("relay_core.listener_engine.mark_processed_batch_with_orders")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_rows", return_value={})
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
        # Verify relay-prefixed exec IDs paired with the orderId
        mark_args = mock_mark.call_args[0]
        self.assertEqual(mark_args[0], mock_conn)
        self.assertEqual(mark_args[1], [("ibkr:0001", "12345")])
        mock_conn.close.assert_called_once()

    @patch("relay_core.listener_engine.mark_processed_batch_with_orders")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_rows", return_value={})
    @patch("relay_core.listener_engine._init_dedup_db")
    def test_dedup_checks_prefixed_ids(
        self,
        mock_init_db: MagicMock,
        mock_get_ids: MagicMock,
        mock_notify: MagicMock,
        mock_mark: MagicMock,
    ) -> None:
        """get_processed_rows is called with relay-prefixed candidate IDs."""
        mock_conn = MagicMock()
        mock_init_db.return_value = mock_conn

        fill = _make_fill(exec_id="X1")
        _send_and_mark("ibkr", [fill], "/tmp/test.db")

        get_ids_args = mock_get_ids.call_args[0]
        self.assertEqual(get_ids_args[1], {"ibkr:X1"})

    @patch("relay_core.listener_engine.mark_processed_batch_with_orders")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_rows")
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
        mock_get_ids.return_value = {"ibkr:0001": "12345"}

        fill = _make_fill()
        _send_and_mark("ibkr", [fill], "/tmp/test.db")

        mock_notify.assert_not_called()
        mock_mark.assert_not_called()
        mock_conn.close.assert_called_once()

    @patch("relay_core.listener_engine.mark_processed_batch_with_orders")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_rows", return_value={})
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

    @patch("relay_core.listener_engine.mark_processed_batch_with_orders")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_rows", return_value={})
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

    @patch("relay_core.listener_engine.mark_processed_batch_with_orders")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_rows", return_value={})
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

    @patch("relay_core.listener_engine.mark_processed_batch_with_orders")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_rows")
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
        mock_get_ids.return_value = {"ibkr:0001": "12345"}

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


# ── _send_and_mark dedup-alias tests (real SQLite) ──────────────────


def _truncate_alias(exec_id: str) -> list[str]:
    """IBKR-style alias: 5-segment TWS combo-leg ID -> 4-segment Flex form."""
    parts = exec_id.split(".")
    return [".".join(parts[:4])] if len(parts) == 5 else []


class TestSendAndMarkDedupAliases(unittest.TestCase):
    """Cross-path dedup via ``BrokerRelay.dedup_aliases`` against a real DB.

    Reproduces the production duplicate: TWS reports combo-leg executions
    with a 5-segment execId while Flex reports the same execution truncated
    to 4 segments — without aliases each path misses the other's row and
    re-delivers the fill.
    """

    _TWS_ID = "0000fb0a.6a91b9f0.02.01.01"
    _FLEX_ID = "0000fb0a.6a91b9f0.02.01"

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmp_dir.name) / "test.db")
        # Replace the conftest default relay with one carrying the alias hook.
        _reset()
        init_relays([
            BrokerRelay(name="ibkr", notifiers=[], dedup_aliases=_truncate_alias),
        ])

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()

    @patch("relay_core.listener_engine.notify")
    def test_listener_first_marks_alias_row_for_poller(
        self, mock_notify: MagicMock,
    ) -> None:
        """Listener-first: the alias is marked so the poller's exact-match lookup hits."""
        fill = _make_fill(exec_id=self._TWS_ID, order_id="1909410294")
        _send_and_mark("ibkr", [fill], self._db_path)
        mock_notify.assert_called_once()

        conn = init_db(Path(self._db_path))
        try:
            rows = get_processed_rows(
                conn, {f"ibkr:{self._TWS_ID}", f"ibkr:{self._FLEX_ID}"},
            )
            # Both keys stored; the alias row carries the orderId
            # (listener-written), never NULL.
            self.assertEqual(rows, {
                f"ibkr:{self._TWS_ID}": "1909410294",
                f"ibkr:{self._FLEX_ID}": "1909410294",
            })
        finally:
            conn.close()

    @patch("relay_core.listener_engine.notify")
    def test_without_hook_poller_lookup_misses(
        self, mock_notify: MagicMock,
    ) -> None:
        """Companion regression: no hook, no alias row — the pre-fix behavior."""
        _reset()
        init_relays([BrokerRelay(name="ibkr", notifiers=[])])

        fill = _make_fill(exec_id=self._TWS_ID)
        _send_and_mark("ibkr", [fill], self._db_path)

        conn = init_db(Path(self._db_path))
        try:
            seen = get_processed_ids(conn, {f"ibkr:{self._FLEX_ID}"})
            self.assertEqual(seen, set())
        finally:
            conn.close()

    @patch("relay_core.listener_engine.notify")
    def test_poller_first_suppresses_replayed_fill(
        self, mock_notify: MagicMock,
    ) -> None:
        """Poller-first: a poller-written (NULL order_id) alias row suppresses the WS replay."""
        conn = init_db(Path(self._db_path))
        try:
            mark_processed_batch(conn, [f"ibkr:{self._FLEX_ID}"])
        finally:
            conn.close()

        fill = _make_fill(exec_id=self._TWS_ID)
        _send_and_mark("ibkr", [fill], self._db_path)
        mock_notify.assert_not_called()

    @patch("relay_core.listener_engine.notify")
    def test_listener_alias_row_never_suppresses_sibling(
        self, mock_notify: MagicMock,
    ) -> None:
        """A listener-written alias row (order_id set) must not suppress a
        sibling 5-segment execution sharing the same truncation."""
        first = _make_fill(exec_id="0000fb0a.6a91b9f0.02.01.01")
        sibling = _make_fill(exec_id="0000fb0a.6a91b9f0.02.01.02")
        _send_and_mark("ibkr", [first], self._db_path)
        _send_and_mark("ibkr", [sibling], self._db_path)
        self.assertEqual(mock_notify.call_count, 2)


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

    @patch("relay_core.listener_engine.mark_processed_batch_with_orders")
    @patch("relay_core.listener_engine.notify")
    @patch("relay_core.listener_engine.get_processed_rows", return_value={})
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
            debounce_buf=_immediate_buffer(),
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
            debounce_buf=_immediate_buffer(),
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
            debounce_buf=_immediate_buffer(),
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
            debounce_buf=_immediate_buffer(),
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
            debounce_buf=buf,
        )
        # Fill should be in its orderId bucket, not dispatched directly
        self.assertEqual(buf._buffers[fill.orderId], [fill])
        mock_send.assert_not_called()
        # Cancel the pending timer task so the test exits cleanly.
        timer = buf._flush_tasks.get(fill.orderId)
        if timer is not None and not timer.done():
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer

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
            debounce_buf=_immediate_buffer(),
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
            debounce_buf=_immediate_buffer(),
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
            debounce_buf=buf,
        )

        # mark=True fills buffered in their (shared) orderId bucket
        order_id = fill_a.orderId
        self.assertEqual(
            [f.execId for f in buf._buffers[order_id]], ["A", "C"],
        )
        mock_send_mark.assert_not_called()

        # mark=False fill still dispatched via _send_no_mark
        mock_send_no_mark.assert_called_once()
        no_mark_fills = mock_send_no_mark.call_args[0][1]
        self.assertEqual(len(no_mark_fills), 1)
        self.assertEqual(no_mark_fills[0].execId, "B")

        # Cleanup pending timer
        timer = buf._flush_tasks.get(order_id)
        if timer is not None and not timer.done():
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer

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
            debounce_buf=_immediate_buffer(),
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
            debounce_buf=_immediate_buffer(),
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
            debounce_buf=_immediate_buffer(),
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
            debounce_buf=_immediate_buffer(),
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
            debounce_buf=_immediate_buffer(),
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
            debounce_buf=_immediate_buffer(),
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
            debounce_buf=_immediate_buffer(),
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
            debounce_buf=_immediate_buffer(),
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
            debounce_buf=_immediate_buffer(),
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
            debounce_buf=buf,
        )

        self.assertEqual(buf._buffers[fill.orderId], [fill])
        self.assertEqual(buf._parse_errors, ["bad timestamp"])
        mock_send.assert_not_called()
        # Cleanup pending timer
        timer = buf._flush_tasks.get(fill.orderId)
        if timer is not None and not timer.done():
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer

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
            debounce_buf=buf,
        )

        self.assertEqual(buf._buffers, {})
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
            debounce_buf=buf,
        )

        self.assertEqual(buf._parse_errors, [])
        mock_send.assert_not_called()


# ── DebounceBuffer tests ────────────────────────────────────────────


class TestDebounceBuffer(unittest.IsolatedAsyncioTestCase):
    """Test the per-orderId debounce buffer batching behavior."""

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
        self.assertEqual(buf._buffers[fill.orderId], [fill])

        await buf.flush()
        mock_send.assert_called_once()
        # Verify relay_name passed to _send_and_mark
        call_args = mock_send.call_args[0]
        self.assertEqual(call_args[0], "ibkr")
        self.assertEqual(buf._buffers, {})

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

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_completed_timer_entry_is_removed(
        self, mock_send: MagicMock,
    ) -> None:
        """After a normal timer-driven flush, ``_flush_tasks`` must not
        retain the finished Task — otherwise long-lived listeners with
        many unique orderIds (the Kraken case) leak references forever.
        """
        buf = DebounceBuffer(
            relay_name="ibkr", debounce_ms=20,
            db_path="/tmp/test.db",
        )
        fill = _make_fill(exec_id="LEAK_PROBE", order_id="ORDER_GONE")
        await buf.add(fill)
        self.assertIn("ORDER_GONE", buf._flush_tasks)

        # Wait for the timer to fire and the done-callback to run.
        await asyncio.sleep(0.10)
        mock_send.assert_called_once()
        self.assertNotIn("ORDER_GONE", buf._flush_tasks)

    async def test_fills_during_in_flight_flush_use_latest_quiet_window(
        self,
    ) -> None:
        """When extra fills land for the same orderId while a flush is
        already in-flight, each new fill must still cancel the previous
        pending timer. Otherwise orphan timer tasks pile up AND the next
        flush fires earlier than the latest fill's quiet window allows.
        """
        flush_started = asyncio.Event()
        flush_can_complete = asyncio.Event()

        async def slow_to_thread(*args: Any, **kwargs: Any) -> None:
            flush_started.set()
            await flush_can_complete.wait()

        buf = DebounceBuffer(
            relay_name="ibkr", debounce_ms=10_000,
            db_path="/tmp/test.db",
        )
        order_id = "ORDER_X"
        first = _make_fill(exec_id="FIRST", order_id=order_id)
        second = _make_fill(exec_id="SECOND", order_id=order_id)
        third = _make_fill(exec_id="THIRD", order_id=order_id)
        await buf.add(first)

        with patch("asyncio.to_thread", side_effect=slow_to_thread):
            # Drive the first fill into _flush_order (slow to_thread blocks).
            flush_task = asyncio.create_task(buf.flush())
            await flush_started.wait()

            # First fill is mid-flush; add a second fill and a third
            # fill to the same order while the flush is stalled.
            await buf.add(second)
            timer_after_second = buf._flush_tasks[order_id]
            await buf.add(third)
            timer_after_third = buf._flush_tasks[order_id]

            # Each new fill must replace the previous pending timer with
            # a fresh one — orphan timers from earlier fills must be
            # cancelled so the quiet window is measured from the latest
            # arrival. ``cancel()`` only schedules the cancellation; the
            # task must yield once so the CancelledError can be raised
            # at its ``await asyncio.sleep`` point before ``cancelled()``
            # flips True.
            self.assertIsNot(timer_after_second, timer_after_third)
            await asyncio.sleep(0)
            self.assertTrue(timer_after_second.cancelled())

            flush_can_complete.set()
            await flush_task

        # Cleanup
        timer_after_third.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await timer_after_third

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_cancelled_task_callback_does_not_evict_replacement(
        self, mock_send: MagicMock,
    ) -> None:
        """Replacing a pending timer via ``add()`` cancels the old task;
        its done-callback must not pop the freshly-installed replacement.
        """
        buf = DebounceBuffer(
            relay_name="ibkr", debounce_ms=10_000,
            db_path="/tmp/test.db",
        )
        fill1 = _make_fill(exec_id="A1", order_id="ORDER_X")
        fill2 = _make_fill(exec_id="A2", order_id="ORDER_X")

        await buf.add(fill1)
        await buf.add(fill2)
        replacement = buf._flush_tasks["ORDER_X"]
        # Yield so the cancelled task's done-callback runs.
        await asyncio.sleep(0)
        # The replacement must still be there.
        self.assertIs(buf._flush_tasks.get("ORDER_X"), replacement)

        # Cleanup
        replacement.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await replacement

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_order_complete_flushes_immediately(
        self, mock_send: MagicMock,
    ) -> None:
        """A fill with order_complete=True bypasses the debounce window."""
        buf = DebounceBuffer(
            relay_name="ibkr", debounce_ms=60_000,  # ~never if we wait
            db_path="/tmp/test.db",
        )
        fill = _make_fill(exec_id="DONE")
        await buf.add(fill, order_complete=True)

        # No sleep — the order should have flushed synchronously inside add().
        mock_send.assert_called_once()
        dispatched = mock_send.call_args[0][1]
        self.assertEqual([f.execId for f in dispatched], ["DONE"])
        self.assertEqual(buf._buffers, {})
        self.assertNotIn(fill.orderId, buf._flush_tasks)

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_order_complete_flushes_accumulated_fills(
        self, mock_send: MagicMock,
    ) -> None:
        """order_complete on the last fill flushes every fill buffered for that order."""
        buf = DebounceBuffer(
            relay_name="ibkr", debounce_ms=60_000,
            db_path="/tmp/test.db",
        )
        first = _make_fill(exec_id="P1")
        second = _make_fill(exec_id="P2")
        last = _make_fill(exec_id="DONE")

        await buf.add(first)
        await buf.add(second)
        mock_send.assert_not_called()

        await buf.add(last, order_complete=True)
        mock_send.assert_called_once()
        dispatched = mock_send.call_args[0][1]
        self.assertEqual(
            [f.execId for f in dispatched], ["P1", "P2", "DONE"],
        )

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_orders_have_independent_timers(
        self, mock_send: MagicMock,
    ) -> None:
        """A fill on order B does not reset order A's pending flush task."""
        buf = DebounceBuffer(
            relay_name="ibkr", debounce_ms=10_000,
            db_path="/tmp/test.db",
        )
        fill_a = _make_fill(exec_id="A", order_id="ORDER_A")
        fill_b = _make_fill(exec_id="B", order_id="ORDER_B")

        await buf.add(fill_a)
        task_a = buf._flush_tasks["ORDER_A"]

        await buf.add(fill_b)
        task_b = buf._flush_tasks["ORDER_B"]

        # Adding a fill for ORDER_B must NOT touch ORDER_A's timer.
        self.assertIsNot(task_a, task_b)
        self.assertFalse(task_a.done())
        self.assertFalse(task_b.done())
        mock_send.assert_not_called()

        # Cleanup
        for task in (task_a, task_b):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

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
        self.assertEqual(buf._buffers[fill.orderId], [fill])

        # flush() catches Exception — should not raise
        await buf.flush()
        mock_send.assert_called_once()
        # Fill must be restored
        self.assertEqual([f.execId for f in buf._buffers[fill.orderId]], ["ERR1"])

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

        self.assertEqual([f.execId for f in buf._buffers[fill.orderId]], ["CAN1"])

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_timer_resets_on_subsequent_add_same_order(
        self, mock_send: MagicMock,
    ) -> None:
        """Adding a fill to the same orderId before its window expires cancels
        that order's pending timer and starts a new one — verified via task
        identity so the assertion does not depend on wall-clock sleep precision.
        """
        buf = DebounceBuffer(
            relay_name="ibkr", debounce_ms=10_000,
            db_path="/tmp/test.db",
        )
        fill1 = _make_fill(exec_id="A1")
        fill2 = _make_fill(exec_id="A2")
        order_id = fill1.orderId
        self.assertEqual(fill2.orderId, order_id)

        await buf.add(fill1)
        first_task = buf._flush_tasks[order_id]

        await buf.add(fill2)
        second_task = buf._flush_tasks[order_id]

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

        The in-progress flush has already popped the order's buffer and
        snapshotted its fills, so the newly-added fill must NOT be lost: it
        should sit in the buffer waiting for its own debounce cycle.
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
        order_id = first.orderId
        await buf.add(first)

        with patch("asyncio.to_thread", side_effect=slow_to_thread):
            flush_task = asyncio.create_task(buf.flush())
            await flush_started.wait()

            # Order's buffer has been popped; flush is in-flight
            self.assertNotIn(order_id, buf._buffers)
            self.assertIn(order_id, buf._flushing)

            # Add a new fill for the same order while the flush is mid-flight
            await buf.add(second)
            self.assertEqual(
                [f.execId for f in buf._buffers[order_id]], ["SECOND"],
            )

            flush_can_complete.set()
            await flush_task

        # FIRST was dispatched; SECOND remains buffered for its own cycle
        self.assertEqual(
            [f.execId for f in buf._buffers[order_id]], ["SECOND"],
        )
        self.assertNotIn(order_id, buf._flushing)

        # Cleanup the pending _delayed_flush task scheduled by the second add()
        timer = buf._flush_tasks.get(order_id)
        if timer is not None and not timer.done():
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await timer

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_extend_errors_flushed_with_fills(
        self, mock_send: MagicMock,
    ) -> None:
        """Errors accumulated via extend_errors ride along with the next order flush."""
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

        self.assertEqual([f.execId for f in buf._buffers[fill.orderId]], ["ERR2"])
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


# ── Book-trade cross-path dedup (listener side) ─────────────────────


def _bt_key(f: Fill) -> str | None:
    """Test classifier: fills tagged in raw get an economic key."""
    if not f.raw.get("isBookTrade"):
        return None
    return f"ACCT|{f.symbol}|{f.side.value}|{abs(f.volume):.10g}|{f.price:.10g}"


class TestSendAndMarkBookTrade(unittest.TestCase):
    """Cross-path consume/record semantics inside _send_and_mark."""

    KEY = "ACCT|TSLA|buy|100|335"

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._db_path = str(Path(self._tmp_dir.name) / "test.db")
        get_relay("ibkr").book_trade_key = _bt_key

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()

    def _bt_fill(self) -> Fill:
        fill = _make_fill(
            exec_id="000310dd.6a6aee05.02.01", symbol="TSLA",
            volume=100.0, price=335.0, fee=0.0, order_id="1544644409",
        )
        fill.raw["isBookTrade"] = True
        return fill

    @patch("relay_core.listener_engine.notify")
    def test_consumes_poller_key_and_suppresses(
        self, mock_notify: MagicMock,
    ) -> None:
        # Poller notified the assignment first (the production ordering).
        conn = init_db(Path(self._db_path))
        try:
            mark_book_trade_keys(conn, "ibkr", "poll", [self.KEY])
        finally:
            conn.close()

        _send_and_mark("ibkr", [self._bt_fill()], self._db_path)

        mock_notify.assert_not_called()
        conn = init_db(Path(self._db_path))
        try:
            # Consumed fill exec-marked; poll key row gone.
            self.assertEqual(
                get_processed_ids(conn, {"ibkr:000310dd.6a6aee05.02.01"}),
                {"ibkr:000310dd.6a6aee05.02.01"},
            )
            self.assertEqual(
                get_processed_ids(conn, {f"ibkr:bt:poll:{self.KEY}"}), set(),
            )
        finally:
            conn.close()

    @patch("relay_core.listener_engine.notify")
    def test_notifies_and_records_ws_key_when_first(
        self, mock_notify: MagicMock,
    ) -> None:
        _send_and_mark("ibkr", [self._bt_fill()], self._db_path)

        mock_notify.assert_called_once()
        conn = init_db(Path(self._db_path))
        try:
            self.assertEqual(
                get_processed_ids(conn, {f"ibkr:bt:ws:{self.KEY}"}),
                {f"ibkr:bt:ws:{self.KEY}"},
            )
        finally:
            conn.close()

    @patch("relay_core.listener_engine.notify")
    def test_own_ws_key_never_self_consumes(
        self, mock_notify: MagicMock,
    ) -> None:
        conn = init_db(Path(self._db_path))
        try:
            mark_book_trade_keys(conn, "ibkr", "ws", [self.KEY])
        finally:
            conn.close()

        _send_and_mark("ibkr", [self._bt_fill()], self._db_path)

        mock_notify.assert_called_once()


# ── Delivery policy: stale fills, retries, drops (R1/R2/R6) ──────────

_NOW = float(to_epoch("2026-10-02T14:30:00"))
_FRESH = "2026-10-02T14:00:00"
_STALE = "2026-05-21T08:27:00"


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://hooks.example.com/x")
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request,
        response=httpx.Response(status, request=request),
    )


def _notification_error(*statuses: int) -> NotificationError:
    return NotificationError(
        [(f"Notifier{i}", _http_error(s)) for i, s in enumerate(statuses)],
    )


def _policy_buffer(
    debounce_ms: int = 5000,
    delays: tuple[float, ...] = (0.01, 0.01),
    clock: Any = None,
) -> DebounceBuffer:
    return DebounceBuffer(
        relay_name="ibkr", debounce_ms=debounce_ms, db_path="/tmp/test.db",
        max_fill_age_s=24 * 3600, retry_delays_s=delays,
        clock=clock or (lambda: _NOW),
    )


async def _drain(buf: DebounceBuffer) -> None:
    """Wait for the buffer's fire-and-forget alert tasks."""
    await asyncio.gather(*buf._background_tasks)


class TestSplitStale(unittest.TestCase):
    def test_partitions_on_cutoff(self) -> None:
        fresh = _make_fill(exec_id="F", timestamp=_FRESH)
        stale = _make_fill(exec_id="S", timestamp=_STALE)
        self.assertEqual(_split_stale([fresh, stale], 24 * 3600, _NOW), ([fresh], [stale]))

    def test_exactly_max_age_is_fresh(self) -> None:
        fill = _make_fill(timestamp="2026-10-01T14:30:00")
        self.assertEqual(_split_stale([fill], 24 * 3600, _NOW), ([fill], []))

    def test_undatable_fills_are_kept_and_logged(self) -> None:
        bad = _make_fill(exec_id="BAD", timestamp="20260411-10:30:00")
        empty = _make_fill(exec_id="EMPTY", timestamp="")
        with self.assertLogs("relay_core.listener_engine", level="ERROR") as logs:
            fresh, stale = _split_stale([bad, empty], 24 * 3600, _NOW)
        self.assertEqual((fresh, stale), ([bad, empty], []))
        self.assertEqual(len(logs.records), 2)


class TestMergeFills(unittest.TestCase):
    def test_newer_copy_wins_at_first_position(self) -> None:
        a1 = _make_fill(exec_id="A", fee=1.0)
        b = _make_fill(exec_id="B")
        a2 = _make_fill(exec_id="A", fee=2.0)
        self.assertEqual(_merge_fills([a1, b], [a2]), [a2, b])


class TestListenerPolicyEnv(unittest.TestCase):
    def test_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            for var in (
                "IBKR_LISTENER_MAX_FILL_AGE_HOURS", "LISTENER_MAX_FILL_AGE_HOURS",
                "IBKR_LISTENER_RETRY_DELAYS_S", "LISTENER_RETRY_DELAYS_S",
            ):
                os.environ.pop(var, None)
            self.assertEqual(get_max_fill_age_s("ibkr"), 24 * 3600)
            self.assertEqual(get_retry_delays_s("ibkr"), (60, 300, 900))

    def test_prefixed_values_win(self) -> None:
        with patch.dict(os.environ, {
            "LISTENER_MAX_FILL_AGE_HOURS": "48",
            "IBKR_LISTENER_MAX_FILL_AGE_HOURS": "12",
            "LISTENER_RETRY_DELAYS_S": "1,2",
            "IBKR_LISTENER_RETRY_DELAYS_S": " 10, 20 ",
        }):
            self.assertEqual(get_max_fill_age_s("ibkr"), 12 * 3600)
            self.assertEqual(get_retry_delays_s("ibkr"), (10, 20))

    def test_invalid_values_fail_fast(self) -> None:
        cases = [
            ("IBKR_LISTENER_MAX_FILL_AGE_HOURS", "0", get_max_fill_age_s),
            ("IBKR_LISTENER_MAX_FILL_AGE_HOURS", "abc", get_max_fill_age_s),
            ("IBKR_LISTENER_RETRY_DELAYS_S", "60,abc", get_retry_delays_s),
            ("IBKR_LISTENER_RETRY_DELAYS_S", "60,0", get_retry_delays_s),
        ]
        for var, raw, getter in cases:
            with self.subTest(var=var, raw=raw), patch.dict(os.environ, {var: raw}):
                with self.assertRaises(SystemExit) as cm:
                    getter("ibkr")
                self.assertIn(var, str(cm.exception))


@patch("relay_core.listener_engine.send_alert")
class TestDebounceBufferStaleGuard(unittest.IsolatedAsyncioTestCase):
    @patch("relay_core.listener_engine._send_and_mark")
    async def test_stale_fill_is_dropped_on_add(
        self, mock_send: MagicMock, mock_alert: MagicMock,
    ) -> None:
        buf = _policy_buffer()
        await buf.add(_make_fill(timestamp=_STALE, order_id="2117129829"))
        await _drain(buf)
        self.assertEqual(buf._buffers, {})
        self.assertEqual(buf._flush_tasks, {})
        mock_send.assert_not_called()
        mock_alert.assert_called_once()
        kwargs = mock_alert.call_args.kwargs
        self.assertEqual(kwargs["key"], "listener-stale:ibkr")
        self.assertIn("orderId=2117129829", kwargs["body"])

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_fill_aging_out_while_buffered_is_dropped_at_flush(
        self, mock_send: MagicMock, mock_alert: MagicMock,
    ) -> None:
        now = [_NOW]
        buf = _policy_buffer(clock=lambda: now[0])
        await buf.add(_make_fill(timestamp=_FRESH))
        now[0] += 2 * 24 * 3600
        await buf.flush()
        await _drain(buf)
        mock_send.assert_not_called()
        mock_alert.assert_called_once()

    @patch("relay_core.listener_engine._send_no_mark")
    async def test_stale_no_mark_fill_is_not_sent(
        self, mock_send_no_mark: MagicMock, mock_alert: MagicMock,
    ) -> None:
        stale = _make_fill(timestamp=_STALE)

        async def on_msg(data: dict[str, Any]) -> list[OnMessageResult]:
            return [OnMessageResult(fill=stale, mark=False)]

        _set_listener(ListenerConfig(
            connect=_dummy_connect, on_message=on_msg, event_filter=lambda _: True,
        ))
        buf = _policy_buffer()
        await _handle_event("ibkr", {"type": "x"}, debounce_buf=buf)
        await _drain(buf)
        mock_send_no_mark.assert_not_called()
        mock_alert.assert_called_once()


@patch("relay_core.listener_engine.send_alert")
class TestDebounceBufferRetryPolicy(unittest.IsolatedAsyncioTestCase):
    @patch("relay_core.listener_engine._send_and_mark")
    async def test_transient_failure_retries_then_succeeds(
        self, mock_send: MagicMock, mock_alert: MagicMock,
    ) -> None:
        mock_send.side_effect = [_notification_error(503), None]
        buf = _policy_buffer()
        await buf.add(_make_fill(timestamp=_FRESH), order_complete=True)
        self.assertEqual(buf._attempts, {"12345": 1})
        self.assertEqual(len(buf._buffers["12345"]), 1)
        self.assertIn("12345", buf._flush_tasks)

        await asyncio.sleep(0.1)
        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual(buf._buffers, {})
        self.assertEqual(buf._attempts, {})
        mock_alert.assert_not_called()

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_exhausted_retries_drop_and_alert(
        self, mock_send: MagicMock, mock_alert: MagicMock,
    ) -> None:
        mock_send.side_effect = _notification_error(503)
        buf = _policy_buffer(delays=(0.01, 0.01))
        await buf.add(_make_fill(timestamp=_FRESH), order_complete=True)
        await asyncio.sleep(0.15)
        await _drain(buf)
        self.assertEqual(mock_send.call_count, 3)  # first try + 2 retries
        self.assertEqual(buf._buffers, {})
        self.assertEqual(buf._attempts, {})
        mock_alert.assert_called_once()
        self.assertEqual(mock_alert.call_args.kwargs["key"], "listener-dropped:ibkr:12345")

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_4xx_drops_without_retry(
        self, mock_send: MagicMock, mock_alert: MagicMock,
    ) -> None:
        # Pipedream answers 400 once the daily quota is spent.
        mock_send.side_effect = _notification_error(400)
        buf = _policy_buffer()
        await buf.add(_make_fill(timestamp=_FRESH), order_complete=True)
        await _drain(buf)
        mock_send.assert_called_once()
        self.assertEqual(buf._buffers, {})
        self.assertEqual(buf._flush_tasks, {})
        mock_alert.assert_called_once()
        self.assertIn("4xx", mock_alert.call_args.kwargs["body"])

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_unexpected_error_is_retried(
        self, mock_send: MagicMock, mock_alert: MagicMock,
    ) -> None:
        mock_send.side_effect = [RuntimeError("database is locked"), None]
        buf = _policy_buffer()
        await buf.add(_make_fill(timestamp=_FRESH), order_complete=True)
        await asyncio.sleep(0.1)
        self.assertEqual(mock_send.call_count, 2)
        mock_alert.assert_not_called()

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_flush_leaves_orders_in_backoff_to_their_timer(
        self, mock_send: MagicMock, mock_alert: MagicMock,
    ) -> None:
        mock_send.side_effect = _notification_error(503)
        buf = _policy_buffer(delays=(3600,))
        await buf.add(_make_fill(timestamp=_FRESH), order_complete=True)
        await buf.flush()  # e.g. a WS reconnect
        mock_send.assert_called_once()
        self.assertEqual(buf._attempts, {"12345": 1})
        buf._flush_tasks["12345"].cancel()

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_incident_replay_leaves_nothing_to_resend(
        self, mock_send: MagicMock, mock_alert: MagicMock,
    ) -> None:
        """2026-10-02: fills rejected with 400 stayed buffered for months and
        were re-sent on the next WS drop. Now a 4xx empties the buffer, so
        a later disconnect flush sends nothing."""
        mock_send.side_effect = _notification_error(400)
        buf = _policy_buffer()
        for order_id in ("1929947674", "2117129829", "53429659"):
            await buf.add(
                _make_fill(exec_id=order_id, order_id=order_id, timestamp=_FRESH),
                order_complete=True,
            )
        await _drain(buf)
        self.assertEqual(mock_send.call_count, 3)
        self.assertEqual(buf._buffers, {})

        await buf.flush()
        self.assertEqual(mock_send.call_count, 3)

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_duplicate_exec_id_keeps_newer_copy(
        self, mock_send: MagicMock, mock_alert: MagicMock,
    ) -> None:
        buf = _policy_buffer()
        await buf.add(_make_fill(exec_id="A", fee=1.0, timestamp=_FRESH))
        await buf.add(_make_fill(exec_id="A", fee=2.0, timestamp=_FRESH))
        self.assertEqual([f.fee for f in buf._buffers["12345"]], [2.0])
        await buf.flush()
        self.assertEqual(len(mock_send.call_args[0][1]), 1)


@patch("relay_core.listener_engine.send_alert")
class TestDebounceBufferSendNow(unittest.IsolatedAsyncioTestCase):
    @patch("relay_core.listener_engine._send_and_mark")
    async def test_one_batch_per_message(
        self, mock_send: MagicMock, mock_alert: MagicMock,
    ) -> None:
        buf = _policy_buffer(debounce_ms=0)
        fills = [
            _make_fill(exec_id="A", order_id="1", timestamp=_FRESH),
            _make_fill(exec_id="B", order_id="2", timestamp=_FRESH),
        ]
        await buf.send_now(fills, ["parse error"])
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args[0][1], fills)
        self.assertEqual(mock_send.call_args[0][3], ["parse error"])

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_drops_stale_before_sending(
        self, mock_send: MagicMock, mock_alert: MagicMock,
    ) -> None:
        buf = _policy_buffer(debounce_ms=0)
        fresh = _make_fill(exec_id="A", timestamp=_FRESH)
        await buf.send_now([fresh, _make_fill(exec_id="B", timestamp=_STALE)], [])
        await _drain(buf)
        self.assertEqual(mock_send.call_args[0][1], [fresh])
        mock_alert.assert_called_once()

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_all_stale_sends_nothing(
        self, mock_send: MagicMock, mock_alert: MagicMock,
    ) -> None:
        buf = _policy_buffer(debounce_ms=0)
        await buf.send_now([_make_fill(timestamp=_STALE)], ["parse error"])
        await _drain(buf)
        mock_send.assert_not_called()

    @patch("relay_core.listener_engine._send_and_mark")
    async def test_failure_retries_each_order_through_the_buffer(
        self, mock_send: MagicMock, mock_alert: MagicMock,
    ) -> None:
        mock_send.side_effect = [_notification_error(503), None, None]
        buf = _policy_buffer(debounce_ms=0)
        await buf.send_now([
            _make_fill(exec_id="A", order_id="1", timestamp=_FRESH),
            _make_fill(exec_id="B", order_id="2", timestamp=_FRESH),
        ], [])
        self.assertEqual(buf._attempts, {"1": 1, "2": 1})
        await asyncio.sleep(0.1)
        self.assertEqual(mock_send.call_count, 3)  # batch + one retry per order
        self.assertEqual(buf._buffers, {})
        mock_alert.assert_not_called()


class TestIsTransientFailure(unittest.TestCase):
    def test_classification(self) -> None:
        self.assertFalse(is_transient_failure(_notification_error(400)))
        self.assertFalse(is_transient_failure(_notification_error(400, 404)))
        self.assertTrue(is_transient_failure(_notification_error(503)))
        self.assertTrue(is_transient_failure(_notification_error(400, 502)))
        self.assertTrue(is_transient_failure(
            NotificationError([("N", httpx.ReadTimeout("timed out"))]),
        ))
        self.assertTrue(is_transient_failure(RuntimeError("database is locked")))


# ── Listen loop: close logging + background disconnect flush (R4/R5) ──


class _FakeWs:
    """Minimal stand-in for ClientWebSocketResponse: yields then closes."""

    def __init__(self, messages: list[aiohttp.WSMessage], close_code: int) -> None:
        self._messages = messages
        self.close_code = close_code
        self.closed = False

    def __aiter__(self) -> Any:
        return self._iterate()

    async def _iterate(self) -> Any:
        for msg in self._messages:
            yield msg
        self.closed = True

    async def close(self) -> None:
        self.closed = True

    def exception(self) -> None:
        return None


class TestListenLoop(unittest.IsolatedAsyncioTestCase):
    def _run_config(
        self, ws: _FakeWs, events: list[str], fill: Fill | None = None,
    ) -> ListenerConfig:
        calls = 0

        async def connect(session: aiohttp.ClientSession) -> aiohttp.ClientWebSocketResponse:
            nonlocal calls
            calls += 1
            if calls > 1:
                events.append("reconnect")
                raise FatalListenerError("stop the test loop")
            return cast(aiohttp.ClientWebSocketResponse, ws)

        async def on_msg(data: dict[str, Any]) -> list[OnMessageResult]:
            return [OnMessageResult(fill=fill, mark=True)] if fill else []

        return ListenerConfig(
            connect=connect, on_message=on_msg, event_filter=lambda _: True,
            debounce_ms=60_000,
        )

    @patch("relay_core.listener_engine.INITIAL_RETRY_DELAY", 0)
    async def test_server_close_is_logged(self) -> None:
        ws = _FakeWs([], close_code=1001)
        _set_listener(self._run_config(ws, []))
        with self.assertLogs("relay_core.listener_engine", level="WARNING") as logs:
            await _listen("ibkr", "/tmp/test.db")
        self.assertTrue(
            any("WS connection closed (code=1001)" in r.getMessage() for r in logs.records),
        )

    @patch("relay_core.listener_engine.INITIAL_RETRY_DELAY", 0)
    async def test_disconnect_flush_does_not_delay_reconnect(self) -> None:
        events: list[str] = []
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        message = aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps({"type": "x"}), None)
        ws = _FakeWs([message], close_code=1001)
        _set_listener(self._run_config(ws, events, fill=_make_fill(timestamp=now)))

        def slow_send(*args: Any) -> None:
            events.append("send-start")
            time.sleep(0.3)
            events.append("send-end")

        with patch("relay_core.listener_engine._send_and_mark", side_effect=slow_send):
            await _listen("ibkr", "/tmp/test.db")
            await asyncio.sleep(0.5)  # let the background flush finish
        self.assertIn("send-start", events)
        self.assertLess(events.index("reconnect"), events.index("send-end"))
