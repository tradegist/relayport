"""Generic poll engine — broker-agnostic fetch/parse/dedup/notify cycle.

The engine receives callbacks (fetch, parse) via ``PollerConfig`` and handles
all orchestration: timestamp watermarking, dedup, aggregation, notify,
mark-after-notify.  Zero broker knowledge.
"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from relay_core.context import get_relay
from relay_core.dedup import get_processed_ids, mark_processed_batch, prune
from relay_core.dedup import init_db as _init_dedup_db
from relay_core.env import get_env, get_env_int
from relay_core.notifier import notify
from relay_core.notifier.models import WebhookPayloadTrades
from shared import Fill, RelayName, Trade, aggregate_fills

log = logging.getLogger(__name__)


# ── Poller configuration ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PollerConfig:
    """Everything the generic poll engine needs from a broker adapter.

    *fetch*: callable that returns raw data (XML, JSON, …) or None on failure.
    *parse*: callable that turns the raw data into (fills, errors).
    *interval*: seconds between poll cycles.
    """

    fetch: Callable[[], str | None]
    parse: Callable[[str], tuple[list[Fill], list[str]]]
    interval: int


# ── Relay-agnostic poller env var getters ────────────────────────────


def get_poll_interval(relay_name: RelayName) -> int:
    """Read {RELAY}_POLL_INTERVAL, falling back to POLL_INTERVAL."""
    prefix = f"{relay_name.upper()}_"
    _, val = get_env_int("POLL_INTERVAL", prefix, default="600")
    return val


def is_poller_enabled(relay_name: RelayName) -> bool:
    """Check {RELAY}_POLLER_ENABLED, falling back to POLLER_ENABLED.

    Defaults to True (polling is on unless explicitly disabled).
    """
    prefix = f"{relay_name.upper()}_"
    val = get_env("POLLER_ENABLED", prefix).lower()
    if not val:
        return True
    return val not in ("0", "false", "no")

# ── Shared DB path (all relays, all pollers) ─────────────────────────
DEDUP_DB_PATH = "/data/dedup/fills.db"
META_DB_PATH = "/data/meta/relay.db"


# ── SQLite helpers ───────────────────────────────────────────────────

def init_dedup_db(db_path: str | None = None) -> sqlite3.Connection:
    """Open the shared dedup database (cross-relay, WAL mode)."""
    return _init_dedup_db(Path(db_path or DEDUP_DB_PATH))


def init_meta_db(db_path: str | None = None) -> sqlite3.Connection:
    """Open the shared metadata database (watermarks for all relays)."""
    path = Path(db_path or META_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn


def _meta_key(relay_name: str, poller_index: int) -> str:
    """Build a namespaced metadata key for timestamp watermark."""
    if poller_index == 0:
        return f"{relay_name}:last_poll_ts"
    return f"{relay_name}:{poller_index}:last_poll_ts"


def get_last_poll_ts(
    meta_conn: sqlite3.Connection, relay_name: str, poller_index: int = 0,
) -> str:
    """Return the last processed trade timestamp, or empty string."""
    key = _meta_key(relay_name, poller_index)
    row = meta_conn.execute(
        "SELECT value FROM metadata WHERE key = ?", (key,),
    ).fetchone()
    return row[0] if row else ""


def set_last_poll_ts(
    meta_conn: sqlite3.Connection, ts: str, relay_name: str, poller_index: int = 0,
) -> None:
    """Update the last processed trade timestamp."""
    key = _meta_key(relay_name, poller_index)
    meta_conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
        (key, ts),
    )
    meta_conn.commit()


def _prefix_ids(relay_name: str, exec_ids: list[str]) -> list[str]:
    """Add relay prefix to exec IDs for dedup namespace isolation."""
    return [f"{relay_name}:{eid}" for eid in exec_ids]


def _prefix_id_set(relay_name: str, exec_ids: set[str]) -> set[str]:
    """Add relay prefix to a set of exec IDs."""
    return {f"{relay_name}:{eid}" for eid in exec_ids}


def _strip_prefix(relay_name: str, prefixed_ids: set[str]) -> set[str]:
    """Remove relay prefix from dedup IDs to get original exec IDs."""
    prefix = f"{relay_name}:"
    return {pid[len(prefix):] for pid in prefixed_ids}


def prune_old(dedup_conn: sqlite3.Connection, days: int = 30) -> None:
    prune(dedup_conn, days=days)


# ── Poll cycle ───────────────────────────────────────────────────────

def poll_once(
    relay_name: RelayName,
    poller_index: int = 0,
    dedup_conn: sqlite3.Connection | None = None,
    meta_conn: sqlite3.Connection | None = None,
    debug: bool = False,
    replay: int = 0,
) -> list[Trade]:
    """Run a single poll cycle. Returns list of new aggregated trades.

    Resolves ``PollerConfig``, notifiers, and retry config from the relay
    context via ``relay_name``.
    """
    close_dedup = dedup_conn is None
    close_meta = meta_conn is None
    if dedup_conn is None:
        dedup_conn = init_dedup_db()
    if meta_conn is None:
        meta_conn = init_meta_db()

    relay = get_relay(relay_name)
    config = relay.poller_configs[poller_index]
    notifiers = relay.notifiers
    notify_retries = relay.notify_retries
    notify_retry_delay_ms = relay.notify_retry_delay_ms

    relay_log = logging.getLogger(f"poller.{relay_name}")
    if poller_index > 0:
        relay_log = logging.getLogger(f"poller.{relay_name}.{poller_index}")

    try:
        relay_log.info("Polling...")
        raw_data = config.fetch()
        if raw_data is None:
            return []

        all_fills, parse_errors = config.parse(raw_data)
        relay_log.info("Parsed %d individual fill(s)", len(all_fills))

        if parse_errors:
            for err in parse_errors:
                relay_log.warning("Parse: %s", err)

        if debug:
            print(f"--- Raw data ({relay_name}) ---")
            print(raw_data)
            print(f"--- End raw data ({relay_name}) ---")

        if all_fills:
            fill_times = [f.timestamp for f in all_fills]
            relay_log.info("Trade time range: %s to %s", min(fill_times), max(fill_times))
            for f in all_fills:
                relay_log.info(
                    "  Fill: %s %s dedup=%s timestamp=%s",
                    f.side, f.symbol, f.execId, f.timestamp,
                )

        # Always show a sample of the first aggregated trade for debugging
        all_trades = aggregate_fills(all_fills)
        if all_trades:
            relay_log.debug(
                "Sample trade (first):\n%s",
                all_trades[0].model_dump_json(indent=2, exclude={"raw"}),
            )

        # Pre-filter by timestamp watermark to reduce dedup work
        last_ts = get_last_poll_ts(meta_conn, relay_name, poller_index)
        if last_ts:
            candidates = [f for f in all_fills if f.timestamp >= last_ts]
            relay_log.info(
                "Timestamp pre-filter: %d -> %d candidate(s) (watermark: %s)",
                len(all_fills), len(candidates), last_ts,
            )
            if len(candidates) < len(all_fills):
                filtered = [f for f in all_fills if f.timestamp < last_ts]
                for f in filtered:
                    relay_log.info(
                        "  Filtered out: %s %s timestamp=%s < watermark %s",
                        f.side, f.symbol, f.timestamp, last_ts,
                    )
        else:
            candidates = all_fills
            relay_log.info("No timestamp watermark — processing all %d fill(s)", len(candidates))

        # Dedup remaining candidates against stored exec IDs (prefixed by relay name)
        candidate_ids = {f.execId for f in candidates}
        prefixed_candidates = _prefix_id_set(relay_name, candidate_ids)
        already_seen_prefixed = get_processed_ids(dedup_conn, prefixed_candidates)
        already_seen = _strip_prefix(relay_name, already_seen_prefixed)
        new_fills = [f for f in candidates if f.execId not in already_seen]
        relay_log.info("%d new fill(s) after dedup", len(new_fills))

        if not new_fills:
            if replay and all_fills:
                sorted_fills = sorted(all_fills, key=lambda f: f.timestamp, reverse=True)
                replay_fills = sorted_fills[:replay]
                trades = aggregate_fills(replay_fills)
                relay_log.info(
                    "Replay mode: resending %d fill(s) as %d trade(s)",
                    len(replay_fills), len(trades),
                )
                notify(
                    notifiers,
                    WebhookPayloadTrades(relay=relay_name, data=trades, errors=parse_errors),
                    retries=notify_retries,
                    retry_delay_ms=notify_retry_delay_ms,
                )
                return trades
            relay_log.info("No new fills")
            return []

        # Aggregate only the NEW fills by order
        trades = aggregate_fills(new_fills)
        relay_log.info("Aggregated into %d trade(s)", len(trades))

        for trade in trades:
            relay_log.info(
                "New trade: %s %s %s @ price %s (vol %s, %d fill(s))",
                trade.side, trade.symbol, trade.orderId,
                trade.price, trade.volume, trade.fillCount,
            )

        # Send a single webhook with all trades
        notify(
            notifiers,
            WebhookPayloadTrades(relay=relay_name, data=trades, errors=parse_errors),
            retries=notify_retries,
            retry_delay_ms=notify_retry_delay_ms,
        )

        # Mark all fills as processed after successful webhook (prefixed)
        all_new_ids = [did for t in trades for did in t.execIds]
        mark_processed_batch(dedup_conn, _prefix_ids(relay_name, all_new_ids))

        # Update timestamp watermark to the latest trade time
        max_ts = max(f.timestamp for f in new_fills)
        set_last_poll_ts(meta_conn, max_ts, relay_name, poller_index)
        relay_log.info("Updated timestamp watermark to %s", max_ts)

        relay_log.info("Sent 1 webhook with %d trade(s)", len(trades))
        return trades
    finally:
        if close_dedup:
            dedup_conn.close()
        if close_meta:
            meta_conn.close()
