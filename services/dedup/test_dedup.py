"""Tests for dedup module."""

import sqlite3
import unittest

from dedup import get_processed_ids, is_processed, mark_processed, mark_processed_batch, prune


class TestDedup(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS processed_fills ("
            "  exec_id TEXT PRIMARY KEY,"
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
