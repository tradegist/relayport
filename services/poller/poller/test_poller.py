"""Unit tests for poller.py — parser is mocked (covered by test_flex_parser.py)."""

import sqlite3
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dedup import get_processed_ids, mark_processed_batch
from models_poller import BuySell, Fill, Trade, WebhookPayloadTrades
from poller import (
    get_last_poll_ts,
    init_dedup_db,
    init_meta_db,
    poll_once,
    prune_old,
    set_last_poll_ts,
)

# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture()
def dedup_db() -> sqlite3.Connection:
    """In-memory SQLite dedup database for tests."""
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
    """In-memory SQLite metadata database for tests."""
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


# ═════════════════════════════════════════════════════════════════════════
#  SQLite helpers
# ═════════════════════════════════════════════════════════════════════════

class TestInitDb:
    def test_dedup_creates_table(self) -> None:
        with patch("poller.DEDUP_DB_PATH", ":memory:"):
            db = init_dedup_db()
        tables = {
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "processed_fills" in tables
        db.close()

    def test_meta_creates_table(self) -> None:
        with patch("poller.META_DB_PATH", ":memory:"):
            db = init_meta_db()
        tables = {
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "metadata" in tables
        db.close()


class TestTimestampWatermark:
    def test_get_returns_empty_when_unset(self, meta_db: sqlite3.Connection) -> None:
        assert get_last_poll_ts(meta_db) == ""

    def test_set_and_get(self, meta_db: sqlite3.Connection) -> None:
        set_last_poll_ts(meta_db, "20250403;120000")
        assert get_last_poll_ts(meta_db) == "20250403;120000"

    def test_update_overwrites(self, meta_db: sqlite3.Connection) -> None:
        set_last_poll_ts(meta_db, "20250401;000000")
        set_last_poll_ts(meta_db, "20250403;120000")
        assert get_last_poll_ts(meta_db) == "20250403;120000"


class TestProcessedIds:
    def test_empty_set_returns_empty(self, dedup_db: sqlite3.Connection) -> None:
        assert get_processed_ids(dedup_db, set()) == set()

    def test_unknown_ids_return_empty(self, dedup_db: sqlite3.Connection) -> None:
        assert get_processed_ids(dedup_db, {"X1", "X2"}) == set()

    def test_mark_and_retrieve(self, dedup_db: sqlite3.Connection) -> None:
        mark_processed_batch(dedup_db, ["E1", "E2", "E3"])
        found = get_processed_ids(dedup_db, {"E1", "E3", "E99"})
        assert found == {"E1", "E3"}

    def test_mark_idempotent(self, dedup_db: sqlite3.Connection) -> None:
        mark_processed_batch(dedup_db, ["E1"])
        mark_processed_batch(dedup_db, ["E1"])  # INSERT OR IGNORE
        rows = dedup_db.execute("SELECT COUNT(*) FROM processed_fills").fetchone()
        assert rows[0] == 1


class TestPruneOld:
    def test_prune_removes_old_entries(self, dedup_db: sqlite3.Connection) -> None:
        # Insert an entry with an old timestamp
        dedup_db.execute(
            "INSERT INTO processed_fills (exec_id, processed_at) VALUES (?, datetime('now', '-60 days'))",
            ("OLD1",),
        )
        dedup_db.execute(
            "INSERT INTO processed_fills (exec_id, processed_at) VALUES (?, datetime('now'))",
            ("NEW1",),
        )
        dedup_db.commit()
        prune_old(dedup_db, days=30)
        remaining = {r[0] for r in dedup_db.execute("SELECT exec_id FROM processed_fills").fetchall()}
        assert "OLD1" not in remaining
        assert "NEW1" in remaining


# ═════════════════════════════════════════════════════════════════════════
#  poll_once() — parser mocked
# ═════════════════════════════════════════════════════════════════════════

class TestPollOnce:
    """poll_once with mocked fetch_flex_report and parser."""

    @patch("poller.notify")
    @patch("poller.aggregate_fills")
    @patch("poller.parse_fills")
    @patch("poller.fetch_flex_report")
    def test_no_report_returns_empty(
        self,
        mock_fetch: MagicMock,
        mock_parse: MagicMock,
        mock_agg: MagicMock,
        mock_notify: MagicMock,
        dedup_db: sqlite3.Connection,
        meta_db: sqlite3.Connection,
    ) -> None:
        mock_fetch.return_value = None
        result = poll_once(dedup_db, meta_db)
        assert result == []
        mock_parse.assert_not_called()
        mock_notify.assert_not_called()

    @patch("poller.notify")
    @patch("poller.aggregate_fills")
    @patch("poller.parse_fills")
    @patch("poller.fetch_flex_report")
    def test_no_fills_returns_empty(
        self,
        mock_fetch: MagicMock,
        mock_parse: MagicMock,
        mock_agg: MagicMock,
        mock_notify: MagicMock,
        dedup_db: sqlite3.Connection,
        meta_db: sqlite3.Connection,
    ) -> None:
        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([], [])
        result = poll_once(dedup_db, meta_db)
        assert result == []
        mock_agg.assert_called_once_with([])
        mock_notify.assert_not_called()

    @patch("poller.notify")
    @patch("poller.aggregate_fills")
    @patch("poller.parse_fills")
    @patch("poller.fetch_flex_report")
    def test_new_fills_sent_via_webhook(
        self,
        mock_fetch: MagicMock,
        mock_parse: MagicMock,
        mock_agg: MagicMock,
        mock_notify: MagicMock,
        dedup_db: sqlite3.Connection,
        meta_db: sqlite3.Connection,
    ) -> None:
        fill = _make_fill()
        trade = _make_trade()
        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([fill], [])
        mock_agg.return_value = [trade]

        result = poll_once(dedup_db, meta_db)

        assert len(result) == 1
        assert result[0].symbol == "AAPL"
        mock_notify.assert_called_once()
        sent_payload = mock_notify.call_args[0][1]
        assert isinstance(sent_payload, WebhookPayloadTrades)
        assert len(sent_payload.data) == 1

    @patch("poller.notify")
    @patch("poller.aggregate_fills")
    @patch("poller.parse_fills")
    @patch("poller.fetch_flex_report")
    def test_fills_marked_as_processed(
        self,
        mock_fetch: MagicMock,
        mock_parse: MagicMock,
        mock_agg: MagicMock,
        mock_notify: MagicMock,
        dedup_db: sqlite3.Connection,
        meta_db: sqlite3.Connection,
    ) -> None:
        fill = _make_fill(execId="TX99")
        trade = _make_trade(execIds=["TX99"])
        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([fill], [])
        mock_agg.return_value = [trade]

        poll_once(dedup_db, meta_db)

        # Verify TX99 is now in the DB
        found = get_processed_ids(dedup_db, {"TX99"})
        assert "TX99" in found

    @patch("poller.notify")
    @patch("poller.aggregate_fills")
    @patch("poller.parse_fills")
    @patch("poller.fetch_flex_report")
    def test_duplicate_fills_skipped(
        self,
        mock_fetch: MagicMock,
        mock_parse: MagicMock,
        mock_agg: MagicMock,
        mock_notify: MagicMock,
        dedup_db: sqlite3.Connection,
        meta_db: sqlite3.Connection,
    ) -> None:
        """Fills already in the DB are not re-sent."""
        mark_processed_batch(dedup_db, ["TX1"])

        fill = _make_fill(execId="TX1")
        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([fill], [])
        mock_agg.return_value = []  # all fills filtered → aggregate gets empty

        result = poll_once(dedup_db, meta_db)

        assert result == []
        mock_notify.assert_not_called()

    @patch("poller.notify")
    @patch("poller.aggregate_fills")
    @patch("poller.parse_fills")
    @patch("poller.fetch_flex_report")
    def test_timestamp_watermark_updated(
        self,
        mock_fetch: MagicMock,
        mock_parse: MagicMock,
        mock_agg: MagicMock,
        mock_notify: MagicMock,
        dedup_db: sqlite3.Connection,
        meta_db: sqlite3.Connection,
    ) -> None:
        fill = _make_fill(timestamp="20250403;150000")
        trade = _make_trade(timestamp="20250403;150000", execIds=["TX1"])
        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([fill], [])
        mock_agg.return_value = [trade]

        poll_once(dedup_db, meta_db)

        assert get_last_poll_ts(meta_db) == "20250403;150000"

    @patch("poller.notify")
    @patch("poller.aggregate_fills")
    @patch("poller.parse_fills")
    @patch("poller.fetch_flex_report")
    def test_watermark_pre_filters_old_fills(
        self,
        mock_fetch: MagicMock,
        mock_parse: MagicMock,
        mock_agg: MagicMock,
        mock_notify: MagicMock,
        dedup_db: sqlite3.Connection,
        meta_db: sqlite3.Connection,
    ) -> None:
        """Fills older than the watermark are filtered by timestamp pre-filter."""
        set_last_poll_ts(meta_db, "20250403;120000")

        old_fill = _make_fill(execId="OLD", timestamp="20250403;100000")
        new_fill = _make_fill(execId="NEW", timestamp="20250403;130000")
        trade = _make_trade(execIds=["NEW"])
        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([old_fill, new_fill], [])

        # aggregate_fills is called twice:
        #   1) all fills → sample trade for debug logging
        #   2) new fills only (after watermark + dedup) → actual dispatch
        mock_agg.side_effect = [
            [_make_trade()],   # 1st call: sample from all fills
            [trade],           # 2nd call: only new fills after filtering
        ]

        result = poll_once(dedup_db, meta_db)

        assert len(result) == 1

        # Only NEW should be processed
        found = get_processed_ids(dedup_db, {"OLD", "NEW"})
        assert "NEW" in found
        assert "OLD" not in found

    @patch("poller.notify")
    @patch("poller.aggregate_fills")
    @patch("poller.parse_fills")
    @patch("poller.fetch_flex_report")
    def test_parse_errors_included_in_webhook(
        self,
        mock_fetch: MagicMock,
        mock_parse: MagicMock,
        mock_agg: MagicMock,
        mock_notify: MagicMock,
        dedup_db: sqlite3.Connection,
        meta_db: sqlite3.Connection,
    ) -> None:
        fill = _make_fill()
        trade = _make_trade(execIds=["TX1"])
        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([fill], ["Unknown attr: fakeField"])
        mock_agg.return_value = [trade]

        poll_once(dedup_db, meta_db)

        sent_payload = mock_notify.call_args[0][1]
        assert "Unknown attr: fakeField" in sent_payload.errors

    @patch("poller.notify")
    @patch("poller.aggregate_fills")
    @patch("poller.parse_fills")
    @patch("poller.fetch_flex_report")
    def test_replay_resends_existing_fills(
        self,
        mock_fetch: MagicMock,
        mock_parse: MagicMock,
        mock_agg: MagicMock,
        mock_notify: MagicMock,
        dedup_db: sqlite3.Connection,
        meta_db: sqlite3.Connection,
    ) -> None:
        """replay=N re-sends N fills even if already processed."""
        fill = _make_fill(execId="TX1")
        trade = _make_trade(execIds=["TX1"])
        mark_processed_batch(dedup_db, ["TX1"])  # already seen

        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([fill], [])
        mock_agg.side_effect = [
            [trade],  # first call: all fills for sample trade
            [trade],  # second call: replay aggregate
        ]

        result = poll_once(dedup_db, meta_db, replay=1)

        assert len(result) == 1
        mock_notify.assert_called_once()

    @patch("poller.notify")
    @patch("poller.aggregate_fills")
    @patch("poller.parse_fills")
    @patch("poller.fetch_flex_report")
    def test_replay_selects_most_recent_fills(
        self,
        mock_fetch: MagicMock,
        mock_parse: MagicMock,
        mock_agg: MagicMock,
        mock_notify: MagicMock,
        dedup_db: sqlite3.Connection,
        meta_db: sqlite3.Connection,
    ) -> None:
        """replay=1 should pick the fill with the latest timestamp, not arbitrary."""
        old_fill = _make_fill(execId="TX_OLD", timestamp="20240101;080000")
        new_fill = _make_fill(execId="TX_NEW", timestamp="20250601;120000")
        mark_processed_batch(dedup_db, ["TX_OLD", "TX_NEW"])

        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([old_fill, new_fill], [])
        mock_agg.return_value = [_make_trade(execIds=["TX_NEW"])]

        poll_once(dedup_db, meta_db, replay=1)

        # aggregate_fills is called twice: once for all_fills → sample trade,
        # then once for the replay slice. The replay call should get only TX_NEW.
        replay_call_fills = mock_agg.call_args_list[-1][0][0]
        assert len(replay_call_fills) == 1
        assert replay_call_fills[0].execId == "TX_NEW"

    @patch("poller.notify")
    @patch("poller.aggregate_fills")
    @patch("poller.parse_fills")
    @patch("poller.fetch_flex_report")
    def test_multiple_trades_single_webhook(
        self,
        mock_fetch: MagicMock,
        mock_parse: MagicMock,
        mock_agg: MagicMock,
        mock_notify: MagicMock,
        dedup_db: sqlite3.Connection,
        meta_db: sqlite3.Connection,
    ) -> None:
        """Multiple new trades are batched into a single webhook call."""
        f1 = _make_fill(execId="TX1", orderId="O1", symbol="AAPL")
        f2 = _make_fill(execId="TX2", orderId="O2", symbol="GOOG")
        t1 = _make_trade(orderId="O1", symbol="AAPL", execIds=["TX1"])
        t2 = _make_trade(orderId="O2", symbol="GOOG", execIds=["TX2"])

        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([f1, f2], [])
        mock_agg.return_value = [t1, t2]

        result = poll_once(dedup_db, meta_db)

        assert len(result) == 2
        mock_notify.assert_called_once()
        sent_payload = mock_notify.call_args[0][1]
        assert len(sent_payload.data) == 2


# ═════════════════════════════════════════════════════════════════════════
#  E2E: poll_once with real parser (no mock)
# ═════════════════════════════════════════════════════════════════════════

_AF_XML = (
    "<FlexQueryResponse><FlexStatements><FlexStatement>"
    "<Trades>"
    '<Trade accountId="UXXXXXXX" currency="USD" fxRateToBase="1"'
    ' assetCategory="STK" symbol="AAPL" conid="265598"'
    ' tradeID="111" ibExecID="exec.001" transactionID="TX100"'
    ' ibOrderID="ORD100" transactionType="ExchTrade" exchange="ISLAND"'
    ' buySell="BUY" quantity="10" tradePrice="150.5"'
    ' taxes="0" ibCommission="-1.0" ibCommissionCurrency="USD"'
    ' cost="1505" tradeMoney="1505" proceeds="-1505" netCash="-1506"'
    ' tradeDate="20250403" dateTime="20250403;140000" reportDate="20250403"'
    ' settleDateTarget="20250407" />'
    '<Trade accountId="UXXXXXXX" currency="USD" fxRateToBase="1"'
    ' assetCategory="STK" symbol="AAPL" conid="265598"'
    ' tradeID="222" ibExecID="exec.002" transactionID="TX101"'
    ' ibOrderID="ORD100" transactionType="ExchTrade" exchange="ISLAND"'
    ' buySell="BUY" quantity="5" tradePrice="151.0"'
    ' taxes="0" ibCommission="-0.5" ibCommissionCurrency="USD"'
    ' cost="755" tradeMoney="755" proceeds="-755" netCash="-755.5"'
    ' tradeDate="20250403" dateTime="20250403;140030" reportDate="20250403"'
    ' settleDateTarget="20250407" />'
    "</Trades>"
    "</FlexStatement></FlexStatements></FlexQueryResponse>"
)


class TestPollOnceE2E:
    """End-to-end: poll_once uses the real parser (not mocked)."""

    @patch("poller.notify")
    @patch("poller.fetch_flex_report")
    def test_real_parser_integration(
        self,
        mock_fetch: MagicMock,
        mock_notify: MagicMock,
        dedup_db: sqlite3.Connection,
        meta_db: sqlite3.Connection,
    ) -> None:
        """Two fills for the same order → aggregated into 1 trade with correct values."""
        mock_fetch.return_value = _AF_XML

        trades = poll_once(dedup_db, meta_db)

        assert len(trades) == 1
        t = trades[0]
        assert isinstance(t, Trade)
        assert t.symbol == "AAPL"
        assert t.orderId == "ORD100"
        assert t.fillCount == 2
        assert t.volume == pytest.approx(15.0)
        # Weighted avg price: (10*150.5 + 5*151.0) / 15 = 2260/15
        assert t.price == pytest.approx(2260 / 15, rel=1e-6)
        assert t.fee == pytest.approx(-1.5)
        assert t.execIds == ["exec.001", "exec.002"]

        # Webhook sent with the aggregated trade
        mock_notify.assert_called_once()
        sent_payload = mock_notify.call_args[0][1]
        assert len(sent_payload.data) == 1
        assert sent_payload.errors == []

        # Fills marked as processed
        found = get_processed_ids(dedup_db, {"exec.001", "exec.002"})
        assert found == {"exec.001", "exec.002"}

        # Watermark updated
        assert get_last_poll_ts(meta_db) == "20250403;140030"

    @patch("poller.notify")
    @patch("poller.fetch_flex_report")
    def test_second_poll_skips_duplicates(
        self,
        mock_fetch: MagicMock,
        mock_notify: MagicMock,
        dedup_db: sqlite3.Connection,
        meta_db: sqlite3.Connection,
    ) -> None:
        """Polling the same XML twice produces trades only on the first call."""
        mock_fetch.return_value = _AF_XML

        first = poll_once(dedup_db, meta_db)
        assert len(first) == 1

        second = poll_once(dedup_db, meta_db)
        assert second == []

        # Notification sent only once
        assert mock_notify.call_count == 1
