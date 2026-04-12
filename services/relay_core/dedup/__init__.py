"""SQLite dedup — track processed executions by exec_id.

Shared library used by both the poller and remote-client listener
to avoid dispatching the same fill twice.
"""

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open (or create) the dedup database and return a connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS processed_fills ("
        "  exec_id TEXT PRIMARY KEY,"
        "  processed_at TEXT DEFAULT (datetime('now'))"
        ")"
    )
    conn.commit()
    return conn


def is_processed(conn: sqlite3.Connection, exec_id: str) -> bool:
    """Return True if this exec_id has already been dispatched."""
    row = conn.execute(
        "SELECT 1 FROM processed_fills WHERE exec_id = ?", (exec_id,)
    ).fetchone()
    return row is not None


def get_processed_ids(conn: sqlite3.Connection, exec_ids: set[str]) -> set[str]:
    """Return the subset of exec_ids already in the DB."""
    if not exec_ids:
        return set()
    placeholders = ",".join("?" for _ in exec_ids)
    rows = conn.execute(
        f"SELECT exec_id FROM processed_fills WHERE exec_id IN ({placeholders})",
        list(exec_ids),
    ).fetchall()
    return {r[0] for r in rows}


def mark_processed(conn: sqlite3.Connection, exec_id: str) -> None:
    """Record a single exec_id as processed (idempotent)."""
    conn.execute(
        "INSERT OR IGNORE INTO processed_fills (exec_id) VALUES (?)", (exec_id,)
    )
    conn.commit()


def mark_processed_batch(conn: sqlite3.Connection, exec_ids: list[str]) -> None:
    """Record multiple exec_ids as processed (idempotent)."""
    conn.executemany(
        "INSERT OR IGNORE INTO processed_fills (exec_id) VALUES (?)",
        [(eid,) for eid in exec_ids],
    )
    conn.commit()


def prune(conn: sqlite3.Connection, days: int = 30) -> int:
    """Delete entries older than *days*. Returns count deleted."""
    cur = conn.execute(
        "DELETE FROM processed_fills "
        "WHERE processed_at < datetime('now', ?)",
        (f"-{days} days",),
    )
    conn.commit()
    deleted = cur.rowcount
    if deleted:
        log.info("Pruned %d dedup entries older than %d days", deleted, days)
    return deleted
