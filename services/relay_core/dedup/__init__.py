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

import contextlib
import logging
import sqlite3
from pathlib import Path
from typing import Literal

from relay_core.env import get_env_int

log = logging.getLogger(__name__)

DEDUP_DB_PATH = "/data/dedup/fills.db"

# How long processed-fill rows are kept. A fill replayed after its row was
# pruned looks new and is re-sent — keep this well beyond any feed's
# replay horizon (the listener's max fill age, the Flex query period).
DEFAULT_RETENTION_DAYS = 90


def get_retention_days() -> int:
    """Read DEDUP_RETENTION_DAYS (default 90)."""
    var_name, days = get_env_int(
        "DEDUP_RETENTION_DAYS", default=str(DEFAULT_RETENTION_DAYS),
    )
    if days < 1:
        raise SystemExit(f"Invalid {var_name}={days} — must be >= 1")
    return days

# ── Book-trade cross-path dedup ──────────────────────────────────────
#
# Book trades (option assignment / exercise / expiry) reach a relay
# through both the poller and the listener with completely disjoint
# identifiers (IBKR Flex reports them without an ibExecID and with
# per-leg synthetic order ids; the bridge reports native exec ids under
# the order's permId), so neither exec-id nor order-level dedup can
# match them. Adapters that can classify these fills provide
# ``BrokerRelay.book_trade_key``; the engines then reconcile the two
# paths here via per-fill *economic* keys.
#
# Key rows are stored in ``processed_fills`` as synthetic exec_ids of
# the form ``{relay}:bt:{src}:{key}`` (``order_id`` NULL). Real broker
# exec ids never start with ``bt:`` — that namespace is reserved by
# convention for these rows. ``src`` is the engine that notified the
# fill; a fill only ever consumes a key written by the OPPOSITE engine,
# so same-path repeats of identical events (e.g. partial assignment in
# equal tranches on consecutive days, seen by the poller only) are
# never suppressed.
#
# Consumption is a single-statement DELETE with a rowcount check —
# atomic across the poller and listener connections, so two engines
# racing for one key row can never both consume it, and a batch holding
# two identical-key fills against one stored row consumes exactly one.

BookTradeSource = Literal["poll", "ws"]

# Maximum age of a key row for it to be consumable. Covers the observed
# cross-path reporting gap for the same event (~48 min; both reports
# land in the same overnight batch cycle) with a wide margin, while
# staying strictly under the ~24 h cadence of consecutive-night
# assignment tranches so identical distinct events can never
# cross-consume. A larger gap (path down for a day+) fails open to a
# duplicate webhook, never to a dropped fill.
BOOK_TRADE_WINDOW_SECONDS = 18 * 3600


def _book_trade_row_id(relay_name: str, src: BookTradeSource, key: str) -> str:
    return f"{relay_name}:bt:{src}:{key}"


def consume_book_trade_keys(
    conn: sqlite3.Connection,
    relay_name: str,
    own_src: BookTradeSource,
    items: list[tuple[str, str]],
) -> set[str]:
    """Consume opposite-engine book-trade keys; return consumed exec_ids.

    *items* holds ``(exec_id, key)`` pairs for the classified fills of a
    batch. For each pair, atomically delete the opposite-source key row
    if one exists within :data:`BOOK_TRADE_WINDOW_SECONDS`; on success
    the fill's own exec_id is marked processed in the same transaction
    and the exec_id is included in the returned set — the caller must
    drop those fills without notifying.

    Marking without notifying is a deliberate, documented exception to
    the mark-after-notify rule: an opposite-source key row is only ever
    written AFTER a successful notify of a fill with an identical
    economic key, so the consumed fill's content is already delivered.
    """
    other_src: BookTradeSource = "ws" if own_src == "poll" else "poll"
    consumed: set[str] = set()
    for exec_id, key in items:
        cur = conn.execute(
            "DELETE FROM processed_fills WHERE exec_id = ? AND processed_at > datetime('now', ?)",
            (
                _book_trade_row_id(relay_name, other_src, key),
                f"-{BOOK_TRADE_WINDOW_SECONDS} seconds",
            ),
        )
        if cur.rowcount == 1:
            conn.execute(
                "INSERT OR IGNORE INTO processed_fills (exec_id) VALUES (?)",
                (f"{relay_name}:{exec_id}",),
            )
            consumed.add(exec_id)
        conn.commit()
    return consumed


def mark_book_trade_keys(
    conn: sqlite3.Connection,
    relay_name: str,
    own_src: BookTradeSource,
    keys: list[str],
) -> None:
    """Record this engine's book-trade keys after a successful notify.

    ``INSERT OR REPLACE`` (not ``OR IGNORE``): a stale row with the same
    key must get a fresh ``processed_at``, otherwise its old timestamp
    would put the new event outside the consume window. The primary-key
    collapse of two identical live keys is intentional — worst case one
    duplicate webhook survives, never a dropped fill.
    """
    conn.executemany(
        "INSERT OR REPLACE INTO processed_fills (exec_id) VALUES (?)",
        [(_book_trade_row_id(relay_name, own_src, key),) for key in keys],
    )
    conn.commit()


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
    #
    # The ALTER itself is wrapped because the PRAGMA gate is not
    # process-wide: on first deploy the listener and poller can both
    # observe a missing column before either commits the ADD COLUMN,
    # and the loser of the writer-lock race then raises
    # ``OperationalError("duplicate column name: order_id")``. The
    # suppress only matters during that one-time race window — by the
    # next caller the PRAGMA gate fires and the ALTER is skipped.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(processed_fills)")}
    if "order_id" not in cols:
        with contextlib.suppress(sqlite3.OperationalError):
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


def get_processed_rows(
    conn: sqlite3.Connection, exec_ids: set[str],
) -> dict[str, str | None]:
    """Return ``{exec_id: order_id}`` for the subset of exec_ids already stored.

    Callers that only need membership use :func:`get_processed_ids`. This
    variant also exposes ``order_id`` so the listener engine can tell rows
    written by the poller (``order_id`` NULL) from rows it wrote itself when
    matching dedup aliases — an alias hit must only suppress a fill when the
    poller delivered it first.
    """
    if not exec_ids:
        return {}
    placeholders = ",".join("?" for _ in exec_ids)
    rows = conn.execute(
        f"SELECT exec_id, order_id FROM processed_fills "
        f"WHERE exec_id IN ({placeholders})",
        list(exec_ids),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


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


def prune(conn: sqlite3.Connection, days: int = DEFAULT_RETENTION_DAYS) -> int:
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
