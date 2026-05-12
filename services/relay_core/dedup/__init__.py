"""SQLite dedup — track processed executions by exec_id.

Shared library used by both the poller and remote-client listener
to avoid dispatching the same fill twice.

The ``order_id`` column is populated by the listener only — it stores
the broker's order id alongside the exec id so the poller can recognise
fills already dispatched in real time, even when the broker returns a
different identifier on the REST path (e.g. Kraken issues a fresh
consolidated ``txid`` for multi-match orders that does not match the
per-match ``exec_id`` emitted via the WebSocket).
"""

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

DEDUP_DB_PATH = "/data/dedup/fills.db"


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open (or create) the dedup database and return a connection."""
    path = Path(db_path) if db_path else Path(DEDUP_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS processed_fills ("
        "  exec_id TEXT PRIMARY KEY,"
        "  order_id TEXT,"
        "  processed_at TEXT DEFAULT (datetime('now'))"
        ")"
    )
    # Migrate databases created before order_id existed. ``init_db`` is
    # called on every connection open (one per listener flush), so we
    # check the schema with a cheap read-only PRAGMA before attempting
    # the DDL — otherwise the post-migration steady state would issue
    # an aborting ALTER on every connection, briefly contending for the
    # writer lock for no reason.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(processed_fills)")}
    if "order_id" not in cols:
        conn.execute("ALTER TABLE processed_fills ADD COLUMN order_id TEXT")
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
    """Record multiple exec_ids as processed (idempotent).

    ``order_id`` is left NULL — callers that know the originating order
    should use :func:`mark_processed_batch_with_orders` instead.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO processed_fills (exec_id) VALUES (?)",
        [(eid,) for eid in exec_ids],
    )
    conn.commit()


def mark_processed_batch_with_orders(
    conn: sqlite3.Connection, items: list[tuple[str, str]],
) -> None:
    """Record (exec_id, order_id) pairs as processed (idempotent).

    Used by the listener so the poller can recognise multi-match fills
    already dispatched in real time, even when the broker returns a
    consolidated identifier on the REST path.
    """
    conn.executemany(
        "INSERT OR IGNORE INTO processed_fills (exec_id, order_id) VALUES (?, ?)",
        items,
    )
    conn.commit()


def get_recently_processed_order_ids(
    conn: sqlite3.Connection,
    relay_name: str,
    order_ids: set[str],
    within_seconds: int,
) -> set[str]:
    """Return order_ids the listener processed within the time window.

    ``relay_name`` constrains the lookup to this relay's rows via the
    ``relay:`` prefix on ``exec_id`` (the same convention used elsewhere
    in this package). Order ids stored with NULL — i.e. rows written by
    the poller itself — are never returned.
    """
    if not order_ids:
        return set()
    placeholders = ",".join("?" for _ in order_ids)
    rows = conn.execute(
        f"SELECT DISTINCT order_id FROM processed_fills "
        f"WHERE exec_id LIKE ? "
        f"  AND order_id IN ({placeholders}) "
        f"  AND processed_at > datetime('now', ?)",
        [f"{relay_name}:%", *order_ids, f"-{within_seconds} seconds"],
    ).fetchall()
    return {r[0] for r in rows}


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
