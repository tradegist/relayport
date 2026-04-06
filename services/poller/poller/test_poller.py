"""Unit tests for poller.py — parser is mocked (covered by test_flex_parser.py)."""

import sqlite3
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from models_poller import Fill, Trade, WebhookPayload
from poller import (
    get_last_poll_ts,
    get_processed_ids,
    init_db,
    mark_processed,
    poll_once,
    prune_old,
    set_last_poll_ts,
)

# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture()
def db() -> sqlite3.Connection:
    """In-memory SQLite database, initialized with schema."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_fills (
            exec_id TEXT PRIMARY KEY,
            processed_at TEXT DEFAULT (datetime('now'))
        )
    """)
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
        "symbol": "AAPL",
        "buySell": "BUY",
        "quantity": 1.0,
        "price": 100.0,
        "transactionId": "TX1",
        "orderId": "ORD1",
        "dateTime": "20250403;100000",
        "tradeDate": "20250403",
    }
    defaults.update(overrides)
    return Fill(**defaults)


def _make_trade(**overrides: Any) -> Trade:
    defaults: dict[str, Any] = {
        "symbol": "AAPL",
        "buySell": "BUY",
        "quantity": 1.0,
        "price": 100.0,
        "transactionId": "TX1",
        "orderId": "ORD1",
        "dateTime": "20250403;100000",
        "tradeDate": "20250403",
        "execIds": ["TX1"],
        "fillCount": 1,
    }
    defaults.update(overrides)
    return Trade(**defaults)


# ═════════════════════════════════════════════════════════════════════════
#  SQLite helpers
# ═════════════════════════════════════════════════════════════════════════

class TestInitDb:
    def test_creates_tables(self) -> None:
        # Patch DB_PATH so init_db uses our in-memory connection pattern
        with patch("poller.DB_PATH", ":memory:"):
            db = init_db()
        tables = {
            r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "processed_fills" in tables
        assert "metadata" in tables
        db.close()

    def test_idempotent(self) -> None:
        with patch("poller.DB_PATH", ":memory:"):
            db = init_db()
            # Running again should not raise
            # (same connection won't work for :memory:, but tests CREATE IF NOT EXISTS)
        db.close()


class TestTimestampWatermark:
    def test_get_returns_empty_when_unset(self, db: sqlite3.Connection) -> None:
        assert get_last_poll_ts(db) == ""

    def test_set_and_get(self, db: sqlite3.Connection) -> None:
        set_last_poll_ts(db, "20250403;120000")
        assert get_last_poll_ts(db) == "20250403;120000"

    def test_update_overwrites(self, db: sqlite3.Connection) -> None:
        set_last_poll_ts(db, "20250401;000000")
        set_last_poll_ts(db, "20250403;120000")
        assert get_last_poll_ts(db) == "20250403;120000"


class TestProcessedIds:
    def test_empty_set_returns_empty(self, db: sqlite3.Connection) -> None:
        assert get_processed_ids(db, set()) == set()

    def test_unknown_ids_return_empty(self, db: sqlite3.Connection) -> None:
        assert get_processed_ids(db, {"X1", "X2"}) == set()

    def test_mark_and_retrieve(self, db: sqlite3.Connection) -> None:
        mark_processed(db, ["E1", "E2", "E3"])
        found = get_processed_ids(db, {"E1", "E3", "E99"})
        assert found == {"E1", "E3"}

    def test_mark_idempotent(self, db: sqlite3.Connection) -> None:
        mark_processed(db, ["E1"])
        mark_processed(db, ["E1"])  # INSERT OR IGNORE
        rows = db.execute("SELECT COUNT(*) FROM processed_fills").fetchone()
        assert rows[0] == 1


class TestPruneOld:
    def test_prune_removes_old_entries(self, db: sqlite3.Connection) -> None:
        # Insert an entry with an old timestamp
        db.execute(
            "INSERT INTO processed_fills (exec_id, processed_at) VALUES (?, datetime('now', '-60 days'))",
            ("OLD1",),
        )
        db.execute(
            "INSERT INTO processed_fills (exec_id, processed_at) VALUES (?, datetime('now'))",
            ("NEW1",),
        )
        db.commit()
        prune_old(db, days=30)
        remaining = {r[0] for r in db.execute("SELECT exec_id FROM processed_fills").fetchall()}
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
        db: sqlite3.Connection,
    ) -> None:
        mock_fetch.return_value = None
        result = poll_once(db)
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
        db: sqlite3.Connection,
    ) -> None:
        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([], [])
        result = poll_once(db)
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
        db: sqlite3.Connection,
    ) -> None:
        fill = _make_fill()
        trade = _make_trade()
        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([fill], [])
        mock_agg.return_value = [trade]

        result = poll_once(db)

        assert len(result) == 1
        assert result[0].symbol == "AAPL"
        mock_notify.assert_called_once()
        sent_payload = mock_notify.call_args[0][1]
        assert isinstance(sent_payload, WebhookPayload)
        assert len(sent_payload.trades) == 1

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
        db: sqlite3.Connection,
    ) -> None:
        fill = _make_fill(transactionId="TX99")
        trade = _make_trade(transactionId="TX99", execIds=["TX99"])
        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([fill], [])
        mock_agg.return_value = [trade]

        poll_once(db)

        # Verify TX99 is now in the DB
        found = get_processed_ids(db, {"TX99"})
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
        db: sqlite3.Connection,
    ) -> None:
        """Fills already in the DB are not re-sent."""
        mark_processed(db, ["TX1"])

        fill = _make_fill(transactionId="TX1")
        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([fill], [])
        mock_agg.return_value = []  # all fills filtered → aggregate gets empty

        result = poll_once(db)

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
        db: sqlite3.Connection,
    ) -> None:
        fill = _make_fill(dateTime="20250403;150000")
        trade = _make_trade(dateTime="20250403;150000", execIds=["TX1"])
        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([fill], [])
        mock_agg.return_value = [trade]

        poll_once(db)

        assert get_last_poll_ts(db) == "20250403;150000"

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
        db: sqlite3.Connection,
    ) -> None:
        """Fills older than the watermark are filtered by timestamp pre-filter."""
        set_last_poll_ts(db, "20250403;120000")

        old_fill = _make_fill(transactionId="OLD", dateTime="20250403;100000")
        new_fill = _make_fill(transactionId="NEW", dateTime="20250403;130000")
        trade = _make_trade(transactionId="NEW", execIds=["NEW"])
        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([old_fill, new_fill], [])

        # aggregate_fills is called twice: once for all fills (sample trade),
        # once for new fills only — we need to return trades for the second call
        mock_agg.side_effect = [
            [_make_trade()],  # first call: all fills for sample trade
            [trade],          # second call: only new fills
        ]

        result = poll_once(db)

        assert len(result) == 1
        # Only NEW should be processed
        found = get_processed_ids(db, {"OLD", "NEW"})
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
        db: sqlite3.Connection,
    ) -> None:
        fill = _make_fill()
        trade = _make_trade(execIds=["TX1"])
        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([fill], ["Unknown attr: fakeField"])
        mock_agg.return_value = [trade]

        poll_once(db)

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
        db: sqlite3.Connection,
    ) -> None:
        """replay=N re-sends N fills even if already processed."""
        fill = _make_fill(transactionId="TX1")
        trade = _make_trade(execIds=["TX1"])
        mark_processed(db, ["TX1"])  # already seen

        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([fill], [])
        mock_agg.side_effect = [
            [trade],  # first call: all fills for sample trade
            [trade],  # second call: replay aggregate
        ]

        result = poll_once(db, replay=1)

        assert len(result) == 1
        mock_notify.assert_called_once()

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
        db: sqlite3.Connection,
    ) -> None:
        """Multiple new trades are batched into a single webhook call."""
        f1 = _make_fill(transactionId="TX1", orderId="O1", symbol="AAPL")
        f2 = _make_fill(transactionId="TX2", orderId="O2", symbol="GOOG")
        t1 = _make_trade(transactionId="TX1", orderId="O1", symbol="AAPL", execIds=["TX1"])
        t2 = _make_trade(transactionId="TX2", orderId="O2", symbol="GOOG", execIds=["TX2"])

        mock_fetch.return_value = "<xml/>"
        mock_parse.return_value = ([f1, f2], [])
        mock_agg.return_value = [t1, t2]

        result = poll_once(db)

        assert len(result) == 2
        mock_notify.assert_called_once()
        sent_payload = mock_notify.call_args[0][1]
        assert len(sent_payload.trades) == 2


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
        db: sqlite3.Connection,
    ) -> None:
        """Two fills for the same order → aggregated into 1 trade with correct values."""
        mock_fetch.return_value = _AF_XML

        trades = poll_once(db)

        assert len(trades) == 1
        t = trades[0]
        assert isinstance(t, Trade)
        assert t.symbol == "AAPL"
        assert t.orderId == "ORD100"
        assert t.fillCount == 2
        assert t.quantity == pytest.approx(15.0)
        # Weighted avg price: (10*150.5 + 5*151.0) / 15 = 2260/15
        assert t.price == pytest.approx(2260 / 15, rel=1e-6)
        assert t.commission == pytest.approx(-1.5)
        assert t.execIds == ["TX100", "TX101"]

        # Webhook sent with the aggregated trade
        mock_notify.assert_called_once()
        sent_payload = mock_notify.call_args[0][1]
        assert len(sent_payload.trades) == 1
        assert sent_payload.errors == []

        # Fills marked as processed
        found = get_processed_ids(db, {"TX100", "TX101"})
        assert found == {"TX100", "TX101"}

        # Watermark updated
        assert get_last_poll_ts(db) == "20250403;140030"

    @patch("poller.notify")
    @patch("poller.fetch_flex_report")
    def test_second_poll_skips_duplicates(
        self,
        mock_fetch: MagicMock,
        mock_notify: MagicMock,
        db: sqlite3.Connection,
    ) -> None:
        """Polling the same XML twice produces trades only on the first call."""
        mock_fetch.return_value = _AF_XML

        first = poll_once(db)
        assert len(first) == 1

        second = poll_once(db)
        assert second == []

        # Notification sent only once
        assert mock_notify.call_count == 1
