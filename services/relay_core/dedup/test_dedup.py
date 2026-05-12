"""Tests for dedup module."""

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from relay_core.dedup import (
    get_processed_ids,
    get_recently_processed_order_ids,
    init_db,
    is_processed,
    mark_processed,
    mark_processed_batch,
    mark_processed_batch_with_orders,
    prune,
)


class TestDedup(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS processed_fills ("
            "  exec_id TEXT PRIMARY KEY,"
            "  order_id TEXT,"
            "  processed_at TEXT DEFAULT (datetime('now'))"
            ")"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_new_exec_id_not_processed(self) -> None:
        assert not is_processed(self.conn, "TX001")

    def test_mark_and_check(self) -> None:
        mark_processed(self.conn, "TX001")
        assert is_processed(self.conn, "TX001")

    def test_mark_idempotent(self) -> None:
        mark_processed(self.conn, "TX001")
        mark_processed(self.conn, "TX001")
        assert is_processed(self.conn, "TX001")

    def test_different_ids_independent(self) -> None:
        mark_processed(self.conn, "TX001")
        assert not is_processed(self.conn, "TX002")

    def test_batch_mark_and_check(self) -> None:
        mark_processed_batch(self.conn, ["E1", "E2", "E3"])
        found = get_processed_ids(self.conn, {"E1", "E3", "E99"})
        assert found == {"E1", "E3"}

    def test_batch_mark_idempotent(self) -> None:
        mark_processed_batch(self.conn, ["E1"])
        mark_processed_batch(self.conn, ["E1"])
        rows = self.conn.execute("SELECT COUNT(*) FROM processed_fills").fetchone()
        assert rows is not None
        assert rows[0] == 1

    def test_get_processed_ids_empty(self) -> None:
        assert get_processed_ids(self.conn, set()) == set()

    def test_get_processed_ids_none_found(self) -> None:
        assert get_processed_ids(self.conn, {"X1", "X2"}) == set()

    def test_prune_old_entries(self) -> None:
        self.conn.execute(
            "INSERT INTO processed_fills (exec_id, processed_at) "
            "VALUES ('OLD001', datetime('now', '-60 days'))"
        )
        self.conn.execute(
            "INSERT INTO processed_fills (exec_id, processed_at) "
            "VALUES ('NEW001', datetime('now'))"
        )
        self.conn.commit()

        deleted = prune(self.conn, days=30)
        assert deleted == 1
        assert not is_processed(self.conn, "OLD001")
        assert is_processed(self.conn, "NEW001")

    def test_prune_nothing_to_delete(self) -> None:
        mark_processed(self.conn, "TX001")
        deleted = prune(self.conn, days=30)
        assert deleted == 0


class TestMarkProcessedBatchWithOrders(unittest.TestCase):
    """``mark_processed_batch_with_orders`` stores both columns."""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS processed_fills ("
            "  exec_id TEXT PRIMARY KEY,"
            "  order_id TEXT,"
            "  processed_at TEXT DEFAULT (datetime('now'))"
            ")"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_records_exec_and_order(self) -> None:
        mark_processed_batch_with_orders(
            self.conn, [("kraken:E1", "O1"), ("kraken:E2", "O1")],
        )
        rows = self.conn.execute(
            "SELECT exec_id, order_id FROM processed_fills ORDER BY exec_id",
        ).fetchall()
        assert rows == [("kraken:E1", "O1"), ("kraken:E2", "O1")]

    def test_is_idempotent(self) -> None:
        mark_processed_batch_with_orders(self.conn, [("kraken:E1", "O1")])
        mark_processed_batch_with_orders(self.conn, [("kraken:E1", "O1")])
        count = self.conn.execute(
            "SELECT COUNT(*) FROM processed_fills",
        ).fetchone()
        assert count is not None
        assert count[0] == 1


class TestGetRecentlyProcessedOrderIds(unittest.TestCase):
    """``get_recently_processed_order_ids`` honours relay prefix + time window."""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS processed_fills ("
            "  exec_id TEXT PRIMARY KEY,"
            "  order_id TEXT,"
            "  processed_at TEXT DEFAULT (datetime('now'))"
            ")"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_returns_orders_processed_in_window(self) -> None:
        mark_processed_batch_with_orders(
            self.conn, [("kraken:E1", "ORDER_A")],
        )
        result = get_recently_processed_order_ids(
            self.conn, "kraken", {"ORDER_A", "ORDER_B"}, within_seconds=120,
        )
        assert result == {"ORDER_A"}

    def test_excludes_orders_outside_window(self) -> None:
        self.conn.execute(
            "INSERT INTO processed_fills (exec_id, order_id, processed_at) "
            "VALUES ('kraken:OLD', 'ORDER_OLD', datetime('now', '-1 hour'))",
        )
        self.conn.commit()
        result = get_recently_processed_order_ids(
            self.conn, "kraken", {"ORDER_OLD"}, within_seconds=120,
        )
        assert result == set()

    def test_isolated_by_relay_prefix(self) -> None:
        mark_processed_batch_with_orders(
            self.conn, [("ibkr:E1", "ORDER_X")],
        )
        result = get_recently_processed_order_ids(
            self.conn, "kraken", {"ORDER_X"}, within_seconds=120,
        )
        assert result == set()

    def test_ignores_rows_with_null_order_id(self) -> None:
        # Poller-marked rows leave order_id NULL — they must never match.
        mark_processed_batch(self.conn, ["kraken:POLLER_ONLY"])
        result = get_recently_processed_order_ids(
            self.conn, "kraken", {"ANY"}, within_seconds=120,
        )
        assert result == set()

    def test_empty_input_returns_empty(self) -> None:
        result = get_recently_processed_order_ids(
            self.conn, "kraken", set(), within_seconds=120,
        )
        assert result == set()


class TestInitDbMigration(unittest.TestCase):
    """``init_db`` adds the ``order_id`` column in-place on existing DBs."""

    def test_migrates_legacy_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "legacy.db"
            # Simulate a pre-migration database.
            conn = sqlite3.connect(str(db))
            conn.execute(
                "CREATE TABLE processed_fills ("
                "  exec_id TEXT PRIMARY KEY,"
                "  processed_at TEXT DEFAULT (datetime('now'))"
                ")"
            )
            conn.execute("INSERT INTO processed_fills (exec_id) VALUES ('LEGACY')")
            conn.commit()
            conn.close()

            # init_db should add the column without losing the row.
            migrated = init_db(db)
            try:
                cols = [
                    row[1]
                    for row in migrated.execute(
                        "PRAGMA table_info(processed_fills)",
                    )
                ]
                assert "order_id" in cols
                rows = migrated.execute(
                    "SELECT exec_id, order_id FROM processed_fills",
                ).fetchall()
                assert rows == [("LEGACY", None)]
            finally:
                migrated.close()

    def test_idempotent_on_already_migrated_db(self) -> None:
        # Calling init_db twice on the same file must not raise.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fresh.db"
            init_db(db).close()
            # Second call exercises the OperationalError swallow path.
            init_db(db).close()


class TestRecentlyProcessedTimeBoundary(unittest.TestCase):
    """Boundary behaviour around the ``within_seconds`` window."""

    def test_just_inside_window(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE processed_fills ("
                "  exec_id TEXT PRIMARY KEY,"
                "  order_id TEXT,"
                "  processed_at TEXT DEFAULT (datetime('now'))"
                ")"
            )
            mark_processed_batch_with_orders(conn, [("kraken:E", "ORDER_RECENT")])
            # SQLite stores second precision; sleep to disambiguate.
            time.sleep(1.1)
            # Within 10 s — should match.
            assert get_recently_processed_order_ids(
                conn, "kraken", {"ORDER_RECENT"}, within_seconds=10,
            ) == {"ORDER_RECENT"}
            # Within 0 s — should not.
            assert get_recently_processed_order_ids(
                conn, "kraken", {"ORDER_RECENT"}, within_seconds=0,
            ) == set()
        finally:
            conn.close()
