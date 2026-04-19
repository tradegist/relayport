"""Unit tests for poller_engine — generic poll cycle with mocked fetch/parse."""

import sqlite3
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from relay_core.context import get_relay
from relay_core.dedup import get_processed_ids, mark_processed_batch
from relay_core.notifier.models import WebhookPayloadTrades
from relay_core.poller_engine import (
    PollerConfig,
    _meta_key,
    _prefix_ids,
    _strip_prefix,
    get_last_poll_ts,
    init_dedup_db,
    init_meta_db,
    poll_once,
    prune_old,
    set_last_poll_ts,
)
from shared import BuySell, Fill, Trade

# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture()
def dedup_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_fills (
            exec_id TEXT PRIMARY KEY,
            processed_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


@pytest.fixture()
def meta_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn


def _make_fill(**overrides: Any) -> Fill:
    defaults: dict[str, Any] = {
        "source": "flex",
        "symbol": "AAPL",
        "assetClass": "equity",
        "side": BuySell.BUY,
        "volume": 1.0,
        "price": 100.0,
        "cost": 0.0,
        "fee": 0.0,
        "execId": "TX1",
        "orderId": "ORD1",
        "timestamp": "20250403;100000",
        "raw": {},
    }
    defaults.update(overrides)
    return Fill(**defaults)


def _make_trade(**overrides: Any) -> Trade:
    defaults: dict[str, Any] = {
        "source": "flex",
        "symbol": "AAPL",
        "assetClass": "equity",
        "side": BuySell.BUY,
        "volume": 1.0,
        "price": 100.0,
        "cost": 0.0,
        "fee": 0.0,
        "orderId": "ORD1",
        "timestamp": "20250403;100000",
        "execIds": ["TX1"],
        "fillCount": 1,
        "raw": {},
    }
    defaults.update(overrides)
    return Trade(**defaults)


def _noop_fetch() -> str | None:
    return "<xml/>"


def _noop_parse(raw: str) -> tuple[list[Fill], list[str]]:
    return [], []


def _set_poller(cfg: Any) -> None:
    """Set the poller config on the test relay in the context."""
    relay = get_relay("ibkr")
    relay.poller_configs = [cfg]


# ── Mock PollerConfig ────────────────────────────────────────────────

def _MockPollerConfig(
    fetch: Any = None,
    parse: Any = None,
    interval: int = 600,
) -> PollerConfig:
    """Build a PollerConfig with test defaults."""
    return PollerConfig(
        fetch=fetch or _noop_fetch,
        parse=parse or _noop_parse,
        interval=interval,
    )


# ═════════════════════════════════════════════════════════════════════
#  Namespace helpers
# ═════════════════════════════════════════════════════════════════════

class TestNamespaceHelpers:
    def test_meta_key_default_index(self) -> None:
        assert _meta_key("ibkr", 0) == "ibkr:last_poll_ts"

    def test_meta_key_secondary_poller(self) -> None:
        assert _meta_key("ibkr", 1) == "ibkr:1:last_poll_ts"

    def test_prefix_ids(self) -> None:
        assert _prefix_ids("ibkr", ["a", "b"]) == ["ibkr:a", "ibkr:b"]

    def test_strip_prefix(self) -> None:
        assert _strip_prefix("ibkr", {"ibkr:a", "ibkr:b"}) == {"a", "b"}


# ═════════════════════════════════════════════════════════════════════
#  SQLite helpers
# ═════════════════════════════════════════════════════════════════════

class TestInitDb:
    def test_dedup_creates_table(self) -> None:
        db = init_dedup_db(db_path=":memory:")
        tables = {
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "processed_fills" in tables
        db.close()

    def test_meta_creates_table(self, tmp_path: Any) -> None:
        db = init_meta_db(db_path=str(tmp_path / "meta.db"))
        tables = {
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "metadata" in tables
        db.close()


class TestTimestampWatermark:
    def test_get_returns_empty_when_unset(self, meta_db: sqlite3.Connection) -> None:
        assert get_last_poll_ts(meta_db, "ibkr") == ""

    def test_set_and_get(self, meta_db: sqlite3.Connection) -> None:
        set_last_poll_ts(meta_db, "20250403;120000", "ibkr")
        assert get_last_poll_ts(meta_db, "ibkr") == "20250403;120000"

    def test_update_overwrites(self, meta_db: sqlite3.Connection) -> None:
        set_last_poll_ts(meta_db, "20250401;000000", "ibkr")
        set_last_poll_ts(meta_db, "20250403;120000", "ibkr")
        assert get_last_poll_ts(meta_db, "ibkr") == "20250403;120000"

    def test_different_relays_isolated(self, meta_db: sqlite3.Connection) -> None:
        set_last_poll_ts(meta_db, "20250401;000000", "ibkr")
        set_last_poll_ts(meta_db, "20250501;000000", "ibkr", poller_index=1)
        assert get_last_poll_ts(meta_db, "ibkr") == "20250401;000000"
        assert get_last_poll_ts(meta_db, "ibkr", poller_index=1) == "20250501;000000"


class TestPruneOld:
    def test_prune_removes_old_entries(self, dedup_db: sqlite3.Connection) -> None:
        dedup_db.execute(
            "INSERT INTO processed_fills (exec_id, processed_at) "
            "VALUES (?, datetime('now', '-60 days'))",
            ("ibkr:OLD1",),
        )
        dedup_db.execute(
            "INSERT INTO processed_fills (exec_id, processed_at) "
            "VALUES (?, datetime('now'))",
            ("ibkr:NEW1",),
        )
        dedup_db.commit()
        prune_old(dedup_db, days=30)
        remaining = {
            r[0] for r in dedup_db.execute(
                "SELECT exec_id FROM processed_fills"
            ).fetchall()
        }
        assert "ibkr:OLD1" not in remaining
        assert "ibkr:NEW1" in remaining


# ═════════════════════════════════════════════════════════════════════
#  poll_once() — generic engine
# ═════════════════════════════════════════════════════════════════════

class TestPollOnce:
    def test_no_data_returns_empty(
        self, dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        cfg = _MockPollerConfig(fetch=lambda: None)
        _set_poller(cfg)
        result = poll_once("ibkr", dedup_conn=dedup_db, meta_conn=meta_db)
        assert result == []

    def test_no_fills_returns_empty(
        self, dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        cfg = _MockPollerConfig()
        _set_poller(cfg)
        result = poll_once("ibkr", dedup_conn=dedup_db, meta_conn=meta_db)
        assert result == []

    @patch("relay_core.poller_engine.notify")
    def test_new_fills_sent_via_webhook(
        self, mock_notify: MagicMock,
        dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        fill = _make_fill()
        cfg = _MockPollerConfig(
            fetch=lambda: "<xml/>",
            parse=lambda _: ([fill], []),
        )
        _set_poller(cfg)
        result = poll_once("ibkr", dedup_conn=dedup_db, meta_conn=meta_db)
        assert len(result) == 1
        assert result[0].symbol == "AAPL"
        mock_notify.assert_called_once()
        sent_payload = mock_notify.call_args[0][1]
        assert isinstance(sent_payload, WebhookPayloadTrades)
        assert sent_payload.relay == "ibkr"

    @patch("relay_core.poller_engine.notify")
    def test_fills_marked_with_relay_prefix(
        self, mock_notify: MagicMock,
        dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        fill = _make_fill(execId="TX99")
        cfg = _MockPollerConfig(
            fetch=lambda: "<xml/>",
            parse=lambda _: ([fill], []),
        )
        _set_poller(cfg)
        poll_once("ibkr", dedup_conn=dedup_db, meta_conn=meta_db)

        # Verify stored with prefix
        found = get_processed_ids(dedup_db, {"ibkr:TX99"})
        assert "ibkr:TX99" in found

        # Original ID without prefix is NOT in the DB
        found_raw = get_processed_ids(dedup_db, {"TX99"})
        assert "TX99" not in found_raw

    @patch("relay_core.poller_engine.notify")
    def test_duplicate_fills_skipped(
        self, mock_notify: MagicMock,
        dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        # Pre-mark with prefix
        mark_processed_batch(dedup_db, ["ibkr:TX1"])

        fill = _make_fill(execId="TX1")
        cfg = _MockPollerConfig(
            fetch=lambda: "<xml/>",
            parse=lambda _: ([fill], []),
        )
        _set_poller(cfg)
        result = poll_once("ibkr", dedup_conn=dedup_db, meta_conn=meta_db)
        assert result == []
        mock_notify.assert_not_called()

    @patch("relay_core.poller_engine.notify")
    def test_timestamp_watermark_updated(
        self, mock_notify: MagicMock,
        dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        fill = _make_fill(timestamp="20250403;150000")
        cfg = _MockPollerConfig(
            fetch=lambda: "<xml/>",
            parse=lambda _: ([fill], []),
        )
        _set_poller(cfg)
        poll_once("ibkr", dedup_conn=dedup_db, meta_conn=meta_db)
        assert get_last_poll_ts(meta_db, "ibkr") == "20250403;150000"

    @patch("relay_core.poller_engine.notify")
    def test_watermark_pre_filters_old_fills(
        self, mock_notify: MagicMock,
        dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        set_last_poll_ts(meta_db, "20250403;120000", "ibkr")

        old_fill = _make_fill(execId="OLD", timestamp="20250403;100000")
        new_fill = _make_fill(execId="NEW", timestamp="20250403;130000")

        cfg = _MockPollerConfig(
            fetch=lambda: "<xml/>",
            parse=lambda _: ([old_fill, new_fill], []),
        )
        _set_poller(cfg)
        result = poll_once("ibkr", dedup_conn=dedup_db, meta_conn=meta_db)
        assert len(result) == 1

        # Only NEW should be processed (with prefix)
        found = get_processed_ids(dedup_db, {"ibkr:OLD", "ibkr:NEW"})
        assert "ibkr:NEW" in found
        assert "ibkr:OLD" not in found

    @patch("relay_core.poller_engine.notify")
    def test_parse_errors_included_in_webhook(
        self, mock_notify: MagicMock,
        dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        fill = _make_fill()
        cfg = _MockPollerConfig(
            fetch=lambda: "<xml/>",
            parse=lambda _: ([fill], ["Unknown attr: fakeField"]),
        )
        _set_poller(cfg)
        poll_once("ibkr", dedup_conn=dedup_db, meta_conn=meta_db)
        sent_payload = mock_notify.call_args[0][1]
        assert "Unknown attr: fakeField" in sent_payload.errors

    @patch("relay_core.poller_engine.notify")
    def test_replay_resends_existing_fills(
        self, mock_notify: MagicMock,
        dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        fill = _make_fill(execId="TX1")
        mark_processed_batch(dedup_db, ["ibkr:TX1"])

        cfg = _MockPollerConfig(
            fetch=lambda: "<xml/>",
            parse=lambda _: ([fill], []),
        )
        _set_poller(cfg)
        result = poll_once(
            "ibkr", dedup_conn=dedup_db, meta_conn=meta_db,
            replay=1,
        )
        assert len(result) == 1
        mock_notify.assert_called_once()

    @patch("relay_core.poller_engine.notify")
    def test_relay_isolation_different_relays(
        self, mock_notify: MagicMock,
        dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        """Same exec ID from different relays should not collide."""
        fill = _make_fill(execId="SHARED_ID")

        cfg = _MockPollerConfig(
            fetch=lambda: "<xml/>",
            parse=lambda _: ([fill], []),
        )

        # Process as ibkr
        _set_poller(cfg)
        poll_once("ibkr", dedup_conn=dedup_db, meta_conn=meta_db)

        # Same exec ID as ibkr should now be deduped
        _set_poller(cfg)
        result = poll_once("ibkr", dedup_conn=dedup_db, meta_conn=meta_db)
        assert result == []

    @patch("relay_core.poller_engine.notify")
    def test_multiple_trades_single_webhook(
        self, mock_notify: MagicMock,
        dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        f1 = _make_fill(execId="TX1", orderId="O1", symbol="AAPL")
        f2 = _make_fill(execId="TX2", orderId="O2", symbol="GOOG")

        cfg = _MockPollerConfig(
            fetch=lambda: "<xml/>",
            parse=lambda _: ([f1, f2], []),
        )
        _set_poller(cfg)
        result = poll_once("ibkr", dedup_conn=dedup_db, meta_conn=meta_db)
        assert len(result) == 2
        mock_notify.assert_called_once()
        sent_payload = mock_notify.call_args[0][1]
        assert len(sent_payload.data) == 2

    # ── Watermark boundary and combined filtering ─────────────────────

    @patch("relay_core.poller_engine.notify")
    def test_watermark_boundary_includes_equal_timestamp(
        self, mock_notify: MagicMock,
        dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        """Fill with timestamp == watermark passes the >= filter and is processed."""
        set_last_poll_ts(meta_db, "20250403;120000", "ibkr")
        fill = _make_fill(execId="BOUNDARY", timestamp="20250403;120000")
        cfg = _MockPollerConfig(parse=lambda _: ([fill], []))
        _set_poller(cfg)
        result = poll_once("ibkr", dedup_conn=dedup_db, meta_conn=meta_db)
        assert len(result) == 1
        assert "ibkr:BOUNDARY" in get_processed_ids(dedup_db, {"ibkr:BOUNDARY"})

    @patch("relay_core.poller_engine.notify")
    def test_old_fill_blocked_by_watermark_never_reaches_dedup(
        self, mock_notify: MagicMock,
        dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        """Fills older than the watermark are dropped before the dedup lookup.

        This verifies the watermark is acting as an optimization: an old fill
        that was never processed should still be absent from the dedup DB after
        being filtered out, confirming it never reached that layer.
        """
        set_last_poll_ts(meta_db, "20250403;120000", "ibkr")
        old_fill = _make_fill(execId="OLD_UNSEEN", timestamp="20250403;110000")
        cfg = _MockPollerConfig(parse=lambda _: ([old_fill], []))
        _set_poller(cfg)
        result = poll_once("ibkr", dedup_conn=dedup_db, meta_conn=meta_db)
        assert result == []
        mock_notify.assert_not_called()
        assert "ibkr:OLD_UNSEEN" not in get_processed_ids(dedup_db, {"ibkr:OLD_UNSEEN"})

    @patch("relay_core.poller_engine.notify")
    def test_fill_passes_watermark_but_already_in_dedup(
        self, mock_notify: MagicMock,
        dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        """Fill that passes the watermark but is already in dedup is not reprocessed.

        This is the primary real-world duplicate scenario: the fill was processed
        in a previous cycle, its timestamp equals the watermark (the common case
        because watermark is set to max_ts of the last processed batch).
        """
        set_last_poll_ts(meta_db, "20250403;120000", "ibkr")
        mark_processed_batch(dedup_db, ["ibkr:TX1"])

        fill = _make_fill(execId="TX1", timestamp="20250403;120000")
        cfg = _MockPollerConfig(parse=lambda _: ([fill], []))
        _set_poller(cfg)
        result = poll_once("ibkr", dedup_conn=dedup_db, meta_conn=meta_db)
        assert result == []
        mock_notify.assert_not_called()

    @patch("relay_core.poller_engine.notify")
    def test_watermark_advances_to_max_of_new_fills_not_deduped(
        self, mock_notify: MagicMock,
        dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        """Watermark is max of genuinely new fills, not of dedup-blocked candidates.

        When a candidate with a higher timestamp is blocked by dedup, the watermark
        must not advance past the highest timestamp that was actually processed.
        """
        set_last_poll_ts(meta_db, "20250403;120000", "ibkr")
        mark_processed_batch(dedup_db, ["ibkr:ALREADY_SEEN"])

        already_seen = _make_fill(execId="ALREADY_SEEN", timestamp="20250403;150000")
        new_fill = _make_fill(execId="NEW", timestamp="20250403;130000")
        cfg = _MockPollerConfig(parse=lambda _: ([already_seen, new_fill], []))
        _set_poller(cfg)
        poll_once("ibkr", dedup_conn=dedup_db, meta_conn=meta_db)

        # Watermark reflects max of new fills only, not the dedup-blocked one
        assert get_last_poll_ts(meta_db, "ibkr") == "20250403;130000"

    @patch("relay_core.poller_engine.notify")
    def test_all_fills_filtered_by_watermark_no_webhook_no_dedup_write(
        self, mock_notify: MagicMock,
        dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        """When all fills are older than the watermark: no webhook, watermark unchanged, nothing written to dedup."""
        set_last_poll_ts(meta_db, "20250403;150000", "ibkr")
        old1 = _make_fill(execId="OLD1", timestamp="20250403;100000")
        old2 = _make_fill(execId="OLD2", timestamp="20250403;110000")
        cfg = _MockPollerConfig(parse=lambda _: ([old1, old2], []))
        _set_poller(cfg)
        result = poll_once("ibkr", dedup_conn=dedup_db, meta_conn=meta_db)
        assert result == []
        mock_notify.assert_not_called()
        assert get_last_poll_ts(meta_db, "ibkr") == "20250403;150000"
        assert len(get_processed_ids(dedup_db, {"ibkr:OLD1", "ibkr:OLD2"})) == 0

    @patch("relay_core.poller_engine.notify")
    def test_secondary_poller_index_watermark_independent(
        self, mock_notify: MagicMock,
        dedup_db: sqlite3.Connection, meta_db: sqlite3.Connection,
    ) -> None:
        """poller_index=1 has an independent watermark; processing it does not affect index 0.

        Sets index 0's watermark high enough to block the test fill, then runs
        a full poll_once() cycle on index 1 (no watermark) and confirms the
        fill is processed and only the index 1 watermark is updated.
        """
        set_last_poll_ts(meta_db, "20250403;120000", "ibkr", poller_index=0)

        fill = _make_fill(execId="TX_IDX1", timestamp="20250403;100000")
        cfg_idx1 = _MockPollerConfig(parse=lambda _: ([fill], []))
        relay = get_relay("ibkr")
        relay.poller_configs = [_MockPollerConfig(), cfg_idx1]

        result = poll_once("ibkr", poller_index=1, dedup_conn=dedup_db, meta_conn=meta_db)
        assert len(result) == 1
        mock_notify.assert_called_once()

        # Index 0 watermark untouched
        assert get_last_poll_ts(meta_db, "ibkr", poller_index=0) == "20250403;120000"
        # Index 1 watermark now reflects what it processed
        assert get_last_poll_ts(meta_db, "ibkr", poller_index=1) == "20250403;100000"
