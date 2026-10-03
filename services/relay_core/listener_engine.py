"""Generic WS listener engine — broker-agnostic event loop.

The engine receives callbacks (``on_message``, ``event_filter``) via
``ListenerConfig`` and handles all orchestration: WebSocket connection,
reconnect with exponential backoff, debounce buffering, dedup, aggregate,
mark-after-notify.  Zero broker knowledge.
"""

import asyncio
import functools
import json
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import aiohttp

from relay_core.alerter import send_alert
from relay_core.context import get_relay
from relay_core.dedup import (
    consume_book_trade_keys,
    get_processed_rows,
    mark_book_trade_keys,
    mark_processed_batch_with_orders,
)
from relay_core.dedup import init_db as _init_dedup_db
from relay_core.env import get_env, get_env_int
from relay_core.fx import enrich_if_enabled
from relay_core.inflight import INFLIGHT_ORDERS
from relay_core.notifier import NotificationError, is_transient_failure, notify
from relay_core.notifier.audit import log_fills
from relay_core.notifier.models import WebhookPayloadTrades
from shared import Fill, RelayName, aggregate_fills, to_epoch

log = logging.getLogger(__name__)


# ── On-message result ────────────────────────────────────────────────


class FatalListenerError(Exception):
    """Raised when a listener encounters an unrecoverable error (e.g. bad credentials).

    The listener loop will stop retrying and shut down when this is raised.
    """


@dataclass(frozen=True, slots=True)
class OnMessageResult:
    """Return type for ListenerConfig.on_message.

    *fill*: the parsed Fill, or None when the event could not be mapped.
    *mark*: if True, use full dedup+notify+mark pipeline;
            if False, fire-and-forget (no dedup, no mark).
    *error*: human-readable reason why *fill* is None — included in the
             webhook payload's ``errors`` field so consumers know a fill
             was dropped and why.  Ignored when *fill* is set.
    *order_complete*: True when this fill closes its order (broker reports
             the order as fully filled). The debounce buffer flushes the
             owning orderId immediately on this signal instead of waiting
             for the quiet-window timer to expire. Relays that cannot
             detect completion leave this False and the timer handles
             flushing.
    """

    fill: Fill | None = None
    mark: bool = True
    error: str | None = None
    order_complete: bool = False


# ── Listener configuration ───────────────────────────────────────────

DEFAULT_MAX_FILL_AGE_HOURS = 24
DEFAULT_RETRY_DELAYS_S: tuple[int, ...] = (60, 300, 900)


@dataclass(frozen=True, slots=True)
class ListenerConfig:
    """Everything the generic WS listener engine needs from a broker adapter.

    *connect*: async callback that receives an ``aiohttp.ClientSession`` and
        returns a fully connected (and subscribed) WebSocket.  Each broker
        owns its connection protocol (auth headers, token exchange, etc.).
    *on_message*: async callback that parses a raw WS JSON dict and returns
        a list of ``OnMessageResult``.  ``fill=None`` means skip; ``mark=True``
        routes through dedup+notify+mark, ``mark=False`` is fire-and-forget.
    *event_filter*: return True if the event should be processed, False to skip.
    *debounce_ms*: milliseconds to buffer fills before flushing (0 = disabled).
    *max_fill_age_s*: fills whose execution time is older than this are
        dropped (logged + alerted) instead of being sent — on receipt and
        again right before each send. Guards against a WS replaying old
        fills, which would otherwise reach the webhook as new trades.
    *retry_delays_s*: backoff before each retry of a send that failed
        transiently (5xx / timeout / network). ``len()`` is the retry
        budget; after it, or on a 4xx, the fills are dropped and alerted.
    """

    connect: Callable[
        [aiohttp.ClientSession],
        Awaitable[aiohttp.ClientWebSocketResponse],
    ]
    on_message: Callable[
        [dict[str, Any]], Awaitable[list[OnMessageResult]]
    ]
    event_filter: Callable[[dict[str, Any]], bool]
    debounce_ms: int = 0
    max_fill_age_s: int = DEFAULT_MAX_FILL_AGE_HOURS * 3600
    retry_delays_s: tuple[int, ...] = DEFAULT_RETRY_DELAYS_S


# ── Relay-agnostic listener env var getters ──────────────────────────


def is_listener_enabled(relay_name: RelayName) -> bool:
    """Check {RELAY}_LISTENER_ENABLED, falling back to LISTENER_ENABLED."""
    prefix = f"{relay_name.upper()}_"
    val = get_env("LISTENER_ENABLED", prefix).lower()
    return val not in ("0", "false", "no", "")


def get_debounce_ms(relay_name: RelayName) -> int:
    """Read {RELAY}_LISTENER_DEBOUNCE_MS, falling back to LISTENER_DEBOUNCE_MS."""
    prefix = f"{relay_name.upper()}_"
    var_name, val = get_env_int("LISTENER_DEBOUNCE_MS", prefix, default="0")
    if val < 0:
        raise SystemExit(f"Invalid {var_name}={val} — must be >= 0")
    return val


def get_max_fill_age_s(relay_name: RelayName) -> int:
    """Read {RELAY}_LISTENER_MAX_FILL_AGE_HOURS (fallback LISTENER_MAX_FILL_AGE_HOURS).

    Returns seconds. Default 24 hours.
    """
    prefix = f"{relay_name.upper()}_"
    var_name, hours = get_env_int(
        "LISTENER_MAX_FILL_AGE_HOURS", prefix,
        default=str(DEFAULT_MAX_FILL_AGE_HOURS),
    )
    if hours < 1:
        raise SystemExit(f"Invalid {var_name}={hours} — must be >= 1")
    return hours * 3600


def get_retry_delays_s(relay_name: RelayName) -> tuple[int, ...]:
    """Read {RELAY}_LISTENER_RETRY_DELAYS_S (fallback LISTENER_RETRY_DELAYS_S).

    Comma-separated seconds to wait before each retry of a failed send,
    e.g. ``60,300,900`` (the default) — three retries, after 1, 5 and
    15 minutes.
    """
    prefix = f"{relay_name.upper()}_"
    raw = get_env("LISTENER_RETRY_DELAYS_S", prefix)
    if not raw:
        return DEFAULT_RETRY_DELAYS_S
    # Name the variable that actually supplied the value in error messages.
    prefixed = f"{prefix}LISTENER_RETRY_DELAYS_S"
    var_name = prefixed if get_env(prefixed) else "LISTENER_RETRY_DELAYS_S"
    try:
        delays = tuple(int(part.strip()) for part in raw.split(","))
    except ValueError:
        raise SystemExit(
            f"Invalid {var_name}={raw!r} — must be comma-separated whole "
            f"seconds, e.g. 60,300,900"
        ) from None
    if any(d < 1 for d in delays):
        raise SystemExit(f"Invalid {var_name}={raw!r} — every delay must be >= 1")
    return delays

# ── Reconnection constants ───────────────────────────────────────────
INITIAL_RETRY_DELAY = 5
MAX_RETRY_DELAY = 300
RETRY_BACKOFF_FACTOR = 2


# ── Namespace helpers (mirror poller_engine pattern) ─────────────────

def _prefix_ids(relay_name: str, fills: list[Fill]) -> set[str]:
    """Build relay-prefixed exec IDs from a list of fills."""
    return {f"{relay_name}:{f.execId}" for f in fills}


# ── Dispatch helpers (blocking IO — run in asyncio.to_thread) ────────

def _send_and_mark(
    relay_name: RelayName,
    fills: list[Fill],
    db_path: str | None,
    parse_errors: list[str] | None = None,
) -> None:
    """Dedup, aggregate, notify, and mark fills as processed.

    All blocking IO (SQLite + HTTP webhooks) in one function.
    Creates a thread-local SQLite connection (never shares across threads).
    Resolves notifiers and retry config from the relay context.

    *parse_errors* are fills that were dropped before reaching this function
    (e.g. bad timestamp format).  They are included in the payload's
    ``errors`` field so consumers know a fill was skipped and why.

    If all notifiers fail, ``NotificationError`` propagates — fills stay
    unprocessed and will be retried on the next event or reconnect.
    """
    relay = get_relay(relay_name)
    alias_fn = relay.dedup_aliases
    conn = _init_dedup_db(db_path)
    _parse_errors = parse_errors or []
    log_fills(relay_name, fills)
    try:
        # Each fill is looked up under its own exec ID plus any relay-declared
        # dedup aliases — alternate IDs the broker's other feed uses for the
        # same execution (e.g. IBKR Flex truncates combo-leg execIds).
        aliases = {
            f.execId: (alias_fn(f.execId) if alias_fn else []) for f in fills
        }
        lookup = _prefix_ids(relay_name, fills) | {
            f"{relay_name}:{alias}" for f in fills for alias in aliases[f.execId]
        }
        seen_rows = get_processed_rows(conn, lookup)

        new_fills: list[Fill] = []
        for f in fills:
            if f"{relay_name}:{f.execId}" in seen_rows:
                continue
            # An alias hit only counts when the stored row was written by the
            # poller (order_id NULL): rows this listener wrote under an alias
            # key carry their orderId, so they never suppress a sibling
            # execution that truncates to the same alias.
            if any(
                key in seen_rows and seen_rows[key] is None
                for key in (f"{relay_name}:{a}" for a in aliases[f.execId])
            ):
                continue
            new_fills.append(f)

        # Book-trade cross-path dedup (mirrors poller_engine — see
        # relay_core.dedup for the semantics). Consumed fills were
        # already notified by the poller; they are exec-marked inside
        # the consume call (documented mark-after-notify exception).
        # Only mark=True fills reach this function, so fire-and-forget
        # execDetailsEvent fills can never insert or consume keys.
        bt_keys: dict[str, str] = {}
        if relay.book_trade_key is not None:
            bt_keys = {
                f.execId: key
                for f in new_fills
                if (key := relay.book_trade_key(f)) is not None
            }
        if bt_keys:
            consumed_ids = consume_book_trade_keys(
                conn, relay_name, "ws", list(bt_keys.items()),
            )
            if consumed_ids:
                # Keys embed the account id — log symbol/side/volume only.
                for f in new_fills:
                    if f.execId in consumed_ids:
                        log.info(
                            "Book-trade dedup: %s %s vol=%s"
                            " (already notified via poller)",
                            f.side.value, f.symbol, f.volume,
                        )
                new_fills = [f for f in new_fills if f.execId not in consumed_ids]

        if not new_fills and not _parse_errors:
            log.debug("All %d fill(s) already processed", len(fills))
            return

        if new_fills:
            log.info(
                "%d new fill(s) after dedup (of %d received)",
                len(new_fills), len(fills),
            )

        trades = aggregate_fills(new_fills)

        fx_errors: list[str] = []
        if trades:
            trades = enrich_if_enabled(trades, fx_errors)
            for trade in trades:
                log.info(
                    "Listener trade: %s %s orderId=%s @ %s (vol %s, %d fill(s))",
                    trade.side.value, trade.symbol, trade.orderId,
                    trade.price, trade.volume, trade.fillCount,
                )

        all_errors = _parse_errors + fx_errors

        # Notifier-dispatch contract: chronological order regardless of the
        # order events arrived in (debounce buffering can shuffle ordering).
        trades.sort(key=lambda t: t.timestamp)

        # Mark-after-notify: notify then mark (never reversed).
        # If notify raises NotificationError, mark is skipped.
        # While notify is in flight nothing is marked yet, so the orders
        # are registered in the in-flight registry — a concurrent poll
        # cycle defers them instead of sending a duplicate webhook.
        payload = WebhookPayloadTrades(relay=relay_name, data=trades, errors=all_errors)
        in_flight_orders = {t.orderId for t in trades}
        INFLIGHT_ORDERS.register(relay_name, in_flight_orders)
        try:
            notify(
                relay.notifiers, payload,
                retries=relay.notify_retries,
                retry_delay_ms=relay.notify_retry_delay_ms,
                relay_name=relay_name,
            )

            # Mark processed AFTER notify (relay-prefixed keys + orderId).
            # The orderId lets the poller suppress duplicate webhooks for
            # multi-match fills where the broker issues a different
            # consolidated identifier on its REST path.
            if trades:
                # Alias keys are marked alongside the real exec IDs (with the
                # same orderId) so the poller's exact-match lookup recognises
                # fills it would otherwise re-deliver under the broker's
                # alternate ID.
                items = [
                    (f"{relay_name}:{key}", t.orderId)
                    for t in trades
                    for eid in t.execIds
                    for key in (eid, *(alias_fn(eid) if alias_fn else []))
                ]
                mark_processed_batch_with_orders(conn, items)
                fill_count = sum(len(t.execIds) for t in trades)
                log.info("Marked %d fill(s) as processed", fill_count)
            if bt_keys:
                mark_book_trade_keys(
                    conn, relay_name, "ws",
                    [bt_keys[f.execId] for f in new_fills if f.execId in bt_keys],
                )
        finally:
            INFLIGHT_ORDERS.release(relay_name, in_flight_orders)
    finally:
        conn.close()


def _send_no_mark(
    relay_name: RelayName,
    fills: list[Fill],
    parse_errors: list[str] | None = None,
) -> None:
    """Aggregate and notify WITHOUT dedup or marking.

    Used for preliminary exec events (fire-and-forget).
    Resolves notifiers and retry config from the relay context.
    """
    relay = get_relay(relay_name)
    _parse_errors = parse_errors or []
    log_fills(relay_name, fills)
    trades = aggregate_fills(fills)

    fx_errors: list[str] = []
    if trades:
        trades = enrich_if_enabled(trades, fx_errors)
        for trade in trades:
            log.info(
                "Listener preliminary: %s %s orderId=%s @ %s (no commission)",
                trade.side.value, trade.symbol, trade.orderId, trade.price,
            )

    all_errors = _parse_errors + fx_errors
    if not trades and not all_errors:
        return

    # Notifier-dispatch contract: chronological order regardless of source order.
    trades.sort(key=lambda t: t.timestamp)

    notify(
        relay.notifiers,
        WebhookPayloadTrades(relay=relay_name, data=trades, errors=all_errors),
        retries=relay.notify_retries,
        retry_delay_ms=relay.notify_retry_delay_ms,
        relay_name=relay_name,
    )


# ── Fill helpers (pure) ──────────────────────────────────────────────

def _split_stale(
    fills: list[Fill], max_age_s: int, now: float,
) -> tuple[list[Fill], list[Fill]]:
    """Partition *fills* into ``(fresh, stale)`` by execution age at *now*.

    A fill whose timestamp cannot be parsed is kept as fresh (and logged):
    every adapter normalises timestamps upstream, so this is a contract
    violation — a possible duplicate webhook beats silently dropping a fill.
    """
    cutoff = now - max_age_s
    fresh: list[Fill] = []
    stale: list[Fill] = []
    for fill in fills:
        try:
            # to_epoch("") returns 0 (the poller's "no watermark"); here an
            # empty timestamp is as undatable as a malformed one.
            if not fill.timestamp:
                raise ValueError("empty timestamp")
            executed_at = to_epoch(fill.timestamp)
        except ValueError as exc:
            log.error(
                "Cannot check the age of execId=%s (%s) — keeping it",
                fill.execId, exc,
            )
            fresh.append(fill)
            continue
        (stale if executed_at < cutoff else fresh).append(fill)
    return fresh, stale


def _merge_fills(older: list[Fill], newer: list[Fill]) -> list[Fill]:
    """Concatenate two batches, keeping one fill per ``execId``.

    The newer copy of a duplicated execId wins but keeps the position of
    its first occurrence. A fill can reach the buffer twice (e.g. a WS
    replay of a fill still waiting for a retry); aggregating both copies
    would double its volume.
    """
    merged: dict[str, Fill] = {}
    for fill in (*older, *newer):
        merged[fill.execId] = fill
    return list(merged.values())


def _describe_fills(fills: list[Fill]) -> str:
    """One line per fill for logs and alerts.

    Deliberately omits ``Fill.raw``, which carries the account id.
    """
    return "\n".join(
        f"- {f.timestamp} {f.side.value} {f.symbol} vol={f.volume} @ {f.price} "
        f"orderId={f.orderId} execId={f.execId}"
        for f in fills
    )


# ── Debounce buffer ──────────────────────────────────────────────────

class DebounceBuffer:
    """Buffer fills per orderId and flush each after a quiet window.

    Each orderId has its own timer that resets on every new fill for
    that order. A fill arriving on order B never delays a flush for
    order A. When the broker signals that an order is fully filled
    (``order_complete=True``) — or when ``debounce_ms`` is 0 — that
    order's buffer is flushed immediately and its timer is cancelled.

    Delivery policy — no fill stays in the buffer indefinitely:

    - **Stale fills are dropped.** A fill executed more than
      ``max_fill_age_s`` ago is dropped (logged + alerted) instead of
      sent — when it arrives, and again right before every send.
    - **Transient failures are retried** (5xx / timeout / network) after
      each delay in ``retry_delays_s``. A retry timer occupies the
      order's timer slot, so a new fill for that order re-arms the
      debounce timer and the retry happens sooner.
    - **Everything else is dropped and alerted**: a 4xx from every
      notifier (the payload was rejected, resending cannot help) or an
      exhausted retry budget. Dropped fills are not marked processed.

    Parse errors are not associated with any particular orderId. They
    accumulate in a flat list and are emitted with whichever order
    flushes next; if no fills are pending they are flushed on the
    next call to :meth:`flush`.

    Public so adapters can reference the type, but created internally
    by ``start_listener``.
    """

    def __init__(
        self,
        relay_name: RelayName,
        debounce_ms: int,
        db_path: str | None,
        *,
        max_fill_age_s: int = DEFAULT_MAX_FILL_AGE_HOURS * 3600,
        retry_delays_s: tuple[float, ...] = DEFAULT_RETRY_DELAYS_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._relay_name = relay_name
        self._debounce_s = debounce_ms / 1000.0
        self._db_path = db_path
        self._max_fill_age_s = max_fill_age_s
        self._retry_delays_s = retry_delays_s
        self._clock = clock
        self._buffers: dict[str, list[Fill]] = {}
        self._flush_tasks: dict[str, asyncio.Task[None]] = {}
        self._flushing: set[str] = set()
        self._parse_errors: list[str] = []
        # Failed send attempts per orderId; present only while a retry is
        # scheduled. Cleared on success and on drop.
        self._attempts: dict[str, int] = {}
        # Fire-and-forget alert sends (blocking HTTP, run in a thread).
        self._background_tasks: set[asyncio.Task[None]] = set()

    @property
    def debouncing(self) -> bool:
        """False when ``debounce_ms`` is 0 — callers then use :meth:`send_now`."""
        return self._debounce_s > 0

    async def send_now(self, fills: list[Fill], parse_errors: list[str]) -> None:
        """Send one WS message's fills immediately, as a single batch.

        The no-debounce path: one webhook per message, as without a
        buffer. The delivery policy still applies — stale fills are
        dropped first, and on failure each order's fills are handed to
        the retry / drop logic (retries then go through the buffer).
        """
        fills = _merge_fills([], self.drop_stale(fills))
        if not fills:
            # Parse errors were already logged by the caller; an
            # errors-only webhook is not sent (same as before buffering).
            return
        try:
            await asyncio.to_thread(
                _send_and_mark, self._relay_name, fills,
                self._db_path, parse_errors,
            )
        except Exception as exc:
            if not isinstance(exc, NotificationError):
                log.exception(
                    "[%s] Unexpected error dispatching %d fill(s)",
                    self._relay_name, len(fills),
                )
            by_order: dict[str, list[Fill]] = {}
            for fill in fills:
                by_order.setdefault(fill.orderId, []).append(fill)
            errors = parse_errors
            for order_id, order_fills in by_order.items():
                self._on_flush_failure(order_id, order_fills, errors, exc)
                errors = []  # parse errors ride with the first order only

    def drop_stale(self, fills: list[Fill]) -> list[Fill]:
        """Return the fresh fills; drop (log + alert) the stale ones."""
        fresh, stale = _split_stale(fills, self._max_fill_age_s, self._clock())
        if stale:
            hours = self._max_fill_age_s // 3600
            self._report_dropped(
                stale,
                headline=f"{len(stale)} stale fill(s) ignored",
                detail=(
                    f"These fills were executed more than {hours}h ago, so the "
                    f"listener did not send them — a WS feed replaying old "
                    f"events usually causes this (e.g. after a reconnect). "
                    f"If one was genuinely never delivered, resend it manually."
                ),
                # One key per relay: a replay burst produces one email per
                # cooldown window, and every fill is still logged.
                alert_key=f"listener-stale:{self._relay_name}",
            )
        return fresh

    async def add(self, fill: Fill, order_complete: bool = False) -> None:
        """Add a fill to its orderId bucket.

        Stale fills are dropped instead (see :meth:`drop_stale`). If
        ``order_complete`` is True or debouncing is disabled, the buffer
        for that orderId is flushed immediately and its timer cancelled.
        Otherwise a fresh quiet-window timer is started (cancelling any
        pending one for the same orderId).
        """
        if not self.drop_stale([fill]):
            return

        order_id = fill.orderId
        bucket = self._buffers.get(order_id, [])
        if any(f.execId == fill.execId for f in bucket):
            log.info(
                "[%s] execId=%s is already buffered for orderId=%s — "
                "keeping the newer copy",
                self._relay_name, fill.execId, order_id,
            )
        self._buffers[order_id] = _merge_fills(bucket, [fill])

        # Cancel the orderId's pending timer if any (debounce or retry).
        # ``_delayed_flush`` removes its own entry from ``_flush_tasks``
        # before entering ``_flush_order``, so anything we find here is
        # guaranteed to still be in its sleep phase — safe to cancel.
        existing = self._flush_tasks.get(order_id)
        if existing is not None and not existing.done():
            existing.cancel()

        if order_complete or self._debounce_s == 0:
            self._flush_tasks.pop(order_id, None)
            await self._flush_order(order_id)
            return

        self._schedule_flush(order_id, self._debounce_s)

    def _schedule_flush(self, order_id: str, delay_s: float) -> None:
        task = asyncio.get_running_loop().create_task(
            self._delayed_flush(order_id, delay_s),
        )
        # Drop the entry once the timer task finishes so completed
        # orderIds don't pile up in the dict (each Kraken order has a
        # fresh orderId, so without this every settled order would
        # leak ~one Task reference forever).
        task.add_done_callback(functools.partial(self._cleanup_flush_task, order_id))
        self._flush_tasks[order_id] = task

    def _has_pending_timer(self, order_id: str) -> bool:
        timer = self._flush_tasks.get(order_id)
        return timer is not None and not timer.done()

    def _cleanup_flush_task(
        self, order_id: str, task: asyncio.Task[None],
    ) -> None:
        """Remove a finished timer task — but only if it is still the
        current one for that orderId. A cancelled task whose slot has
        already been replaced by ``add()`` must not evict the replacement.
        """
        if self._flush_tasks.get(order_id) is task:
            self._flush_tasks.pop(order_id, None)

    def extend_errors(self, errors: list[str]) -> None:
        """Accumulate parse errors to be flushed with the next batch of fills."""
        self._parse_errors.extend(errors)

    async def _delayed_flush(self, order_id: str, delay_s: float) -> None:
        await asyncio.sleep(delay_s)
        # Remove ourselves from the timer slot *before* starting the
        # flush so a concurrent ``add()`` arriving mid-flush can create
        # and cancel new timers without ever finding (and inadvertently
        # cancelling) the in-flight task. The identity check guards
        # against the case where ``add()`` raced ahead and already
        # replaced us with a fresh timer between the sleep returning
        # and this line — in that case we must not evict the new entry.
        current = asyncio.current_task()
        if self._flush_tasks.get(order_id) is current:
            del self._flush_tasks[order_id]
        await self._flush_order(order_id)

    async def _flush_order(self, order_id: str) -> None:
        """Flush a single orderId's buffer.

        Errors accumulated in ``_parse_errors`` ride along with this
        flush — they belong to no particular order so we attach them
        opportunistically.
        """
        fills = self.drop_stale(self._buffers.pop(order_id, []))
        parse_errors = self._parse_errors.copy()
        self._parse_errors.clear()
        if not fills and not parse_errors:
            self._attempts.pop(order_id, None)
            return
        self._flushing.add(order_id)
        try:
            await asyncio.to_thread(
                _send_and_mark, self._relay_name, fills,
                self._db_path, parse_errors,
            )
        except asyncio.CancelledError:
            log.warning(
                "Flush cancelled (orderId=%s) — restoring %d fill(s) to buffer",
                order_id, len(fills),
            )
            self._restore(order_id, fills, parse_errors)
            raise
        except Exception as exc:
            # notify() has already logged a NotificationError per backend;
            # anything else is unexpected and needs its traceback.
            if not isinstance(exc, NotificationError):
                log.exception(
                    "[%s] Unexpected error dispatching %d fill(s) for orderId=%s",
                    self._relay_name, len(fills), order_id,
                )
            self._on_flush_failure(order_id, fills, parse_errors, exc)
        else:
            self._attempts.pop(order_id, None)
        finally:
            self._flushing.discard(order_id)

    def _restore(
        self, order_id: str, fills: list[Fill], parse_errors: list[str],
    ) -> None:
        """Put a failed batch back, merged with fills added meanwhile."""
        self._buffers[order_id] = _merge_fills(fills, self._buffers.get(order_id, []))
        self._parse_errors = parse_errors + self._parse_errors

    def _on_flush_failure(
        self,
        order_id: str,
        fills: list[Fill],
        parse_errors: list[str],
        exc: Exception,
    ) -> None:
        """Schedule a retry for a transient failure, or drop + alert."""
        attempt = self._attempts.get(order_id, 0) + 1
        transient = is_transient_failure(exc)
        if transient and attempt <= len(self._retry_delays_s):
            delay_s = self._retry_delays_s[attempt - 1]
            self._attempts[order_id] = attempt
            self._restore(order_id, fills, parse_errors)
            log.warning(
                "[%s] Failed to dispatch %d fill(s) for orderId=%s "
                "(attempt %d/%d): %s — retrying in %gs",
                self._relay_name, len(fills), order_id,
                attempt, len(self._retry_delays_s) + 1, exc, delay_s,
            )
            # A fill added while the send was in flight has already armed
            # a debounce timer that will flush the restored fills too.
            if not self._has_pending_timer(order_id):
                self._schedule_flush(order_id, delay_s)
            return

        self._attempts.pop(order_id, None)
        if parse_errors:
            log.error(
                "[%s] Discarding %d parse error(s) sent with the dropped batch",
                self._relay_name, len(parse_errors),
            )
        if not fills:
            return
        reason = (
            f"delivery still failing after {attempt} attempt(s)"
            if transient else
            "every notifier rejected the payload (4xx), so it was not retried"
        )
        self._report_dropped(
            fills,
            headline=f"{len(fills)} fill(s) not delivered",
            detail=(
                f"Giving up on orderId={order_id}: {reason}. Last error: "
                f"{exc}. These fills were NOT marked as processed — resend "
                f"them manually if the receiver needs them."
            ),
            # Per order: every lost order gets its own email.
            alert_key=f"listener-dropped:{self._relay_name}:{order_id}",
        )

    def _report_dropped(
        self, fills: list[Fill], *, headline: str, detail: str, alert_key: str,
    ) -> None:
        """Log dropped fills and email the operator (fire-and-forget)."""
        description = _describe_fills(fills)
        log.error("[%s] %s — %s\n%s", self._relay_name, headline, detail, description)
        self._spawn(asyncio.to_thread(
            send_alert,
            subject=f"[relayport] {self._relay_name}: {headline}",
            body=f"{detail}\n\nFills:\n{description}\n",
            key=alert_key,
        ))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def flush(self) -> None:
        """Flush every pending orderId — safe to call when empty.

        Used on listener shutdown / reconnect to drain the buffers. A
        snapshot of the keys is taken first so newly-added fills during
        the loop do not affect iteration. Orders waiting out a retry
        backoff are left to their timer: flushing them now would spend a
        retry attempt early.
        """
        order_ids = list(self._buffers.keys())
        for order_id in order_ids:
            if order_id in self._attempts and self._has_pending_timer(order_id):
                continue
            timer = self._flush_tasks.pop(order_id, None)
            if (
                timer is not None
                and not timer.done()
                and order_id not in self._flushing
            ):
                timer.cancel()
            await self._flush_order(order_id)
        # Drain orphan parse errors (no fills pending).
        if self._parse_errors:
            errors = self._parse_errors.copy()
            self._parse_errors.clear()
            try:
                await asyncio.to_thread(
                    _send_and_mark, self._relay_name, [],
                    self._db_path, errors,
                )
            except Exception:
                log.exception("Failed to dispatch %d parse error(s)", len(errors))
                self._parse_errors = errors + self._parse_errors


# ── Event handler ────────────────────────────────────────────────────

async def _handle_event(
    relay_name: RelayName,
    data: Any,
    debounce_buf: DebounceBuffer,
) -> None:
    """Process a single parsed WS message using adapter callbacks.

    Every ``mark=True`` fill goes through *debounce_buf* — buffered, or
    sent at once via ``send_now`` when debouncing is disabled — so the
    stale-fill guard and the retry / drop policy apply uniformly.
    """
    relay = get_relay(relay_name)
    config = relay.listener_config
    if config is None:
        raise RuntimeError(f"Relay {relay_name!r} has no listener configured")

    # json.loads can return any JSON type — only dicts are valid events.
    if not isinstance(data, dict):
        log.warning(
            "[%s] Ignoring non-dict WS message: %s",
            relay_name, type(data).__name__,
        )
        return

    # Let the adapter decide if this event is relevant
    if not config.event_filter(data):
        return

    results: list[OnMessageResult] = await config.on_message(data)

    mark_fills: list[tuple[Fill, bool]] = []
    no_mark_fills: list[Fill] = []
    parse_errors: list[str] = []

    for result in results:
        if result.fill is None:
            if result.error:
                log.error("[%s] Skipped fill: %s", relay_name, result.error)
                parse_errors.append(result.error)
            continue
        fill = result.fill
        if result.mark:
            log.info(
                "[%s] Fill: %s %s execId=%s fee=%s",
                relay_name, fill.side.value, fill.symbol, fill.execId, fill.fee,
            )
            mark_fills.append((fill, result.order_complete))
        else:
            log.info(
                "[%s] Fill (no-mark): %s %s execId=%s",
                relay_name, fill.side.value, fill.symbol, fill.execId,
            )
            no_mark_fills.append(fill)

    if mark_fills:
        if debounce_buf.debouncing:
            for fill, order_complete in mark_fills:
                await debounce_buf.add(fill, order_complete=order_complete)
            if parse_errors:
                debounce_buf.extend_errors(parse_errors)
        else:
            await debounce_buf.send_now([f for f, _ in mark_fills], parse_errors)
    elif parse_errors:
        # TODO: route to a dedicated error notifier (email, configurable cadence)
        # once that system exists. For now, errors are visible in server logs only.
        pass

    # Fire-and-forget fills skip the buffer but not the stale-fill guard.
    no_mark_fills = debounce_buf.drop_stale(no_mark_fills)
    if no_mark_fills:
        try:
            await asyncio.to_thread(
                _send_no_mark, relay_name, no_mark_fills,
            )
        except Exception:
            log.exception(
                "[%s] Failed to dispatch %d no-mark fill(s)",
                relay_name, len(no_mark_fills),
            )


# ── WebSocket listener loop ─────────────────────────────────────────

def _on_disconnect_flush_done(
    relay_name: RelayName,
    tasks: set[asyncio.Task[None]],
    task: asyncio.Task[None],
) -> None:
    """Done-callback of a background disconnect flush: untrack, surface errors."""
    tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(
            "[%s] Failed to flush debounce buffer on disconnect",
            relay_name, exc_info=exc,
        )


async def _listen(
    relay_name: RelayName,
    db_path: str | None,
) -> None:
    """Connect, process events, reconnect with exponential backoff."""
    relay = get_relay(relay_name)
    config = relay.listener_config
    if config is None:
        raise RuntimeError(f"Relay {relay_name!r} has no listener configured")

    retry_delay = INITIAL_RETRY_DELAY

    debounce_buf = DebounceBuffer(
        relay_name, config.debounce_ms, db_path,
        max_fill_age_s=config.max_fill_age_s,
        retry_delays_s=config.retry_delays_s,
    )
    # Disconnect flushes run in the background (see below); retained here
    # so they are not garbage-collected mid-run.
    flush_tasks: set[asyncio.Task[None]] = set()

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                log.info("[%s] Connecting to WS", relay_name)

                ws = await config.connect(session)
                try:
                    log.info("[%s] Connected to WS", relay_name)
                    retry_delay = INITIAL_RETRY_DELAY

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                event_data = json.loads(msg.data)
                            except json.JSONDecodeError:
                                log.error(
                                    "[%s] Failed to parse WS message: %.200s",
                                    relay_name, msg.data,
                                )
                                continue

                            await _handle_event(relay_name, event_data, debounce_buf)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            log.error("[%s] WS error: %s", relay_name, ws.exception())
                            break
                    else:
                        # aiohttp ends the iteration silently when the
                        # connection closes (server close frame, heartbeat
                        # timeout) — CLOSE messages never reach the loop
                        # body. Log it, or a bridge restart leaves no trace.
                        log.warning(
                            "[%s] WS connection closed (code=%s)",
                            relay_name, ws.close_code,
                        )
                finally:
                    if not ws.closed:
                        await ws.close()

        except FatalListenerError as exc:
            log.error("[%s] Fatal error — stopping listener: %s", relay_name, exc)
            return
        except aiohttp.ClientError as exc:
            log.error("[%s] WS connection error: %s", relay_name, exc)
        except asyncio.CancelledError:
            log.info("[%s] Listener cancelled — shutting down", relay_name)
            await debounce_buf.flush()
            raise
        except Exception:
            log.exception("[%s] Unexpected error in listener", relay_name)

        # Flush buffered fills in the background: sending them can take
        # several seconds per webhook and must not hold up the reconnect.
        flush_task = asyncio.get_running_loop().create_task(debounce_buf.flush())
        flush_tasks.add(flush_task)
        flush_task.add_done_callback(
            functools.partial(_on_disconnect_flush_done, relay_name, flush_tasks),
        )

        log.info("[%s] Reconnecting in %ds...", relay_name, retry_delay)
        await asyncio.sleep(retry_delay)
        retry_delay = min(
            retry_delay * RETRY_BACKOFF_FACTOR, MAX_RETRY_DELAY,
        )


# ── Public API ───────────────────────────────────────────────────────

async def start_listener(
    relay_name: RelayName,
    db_path: str | None = None,
) -> None:
    """Start the WebSocket listener (runs indefinitely with auto-reconnect).

    Resolves ``ListenerConfig`` and notifiers from the relay context.
    This is the only public entry point.
    """
    relay = get_relay(relay_name)
    config = relay.listener_config
    if config is None:
        raise RuntimeError(f"Relay {relay_name!r} has no listener configured")

    log.info(
        "[%s] Listener starting (debounce=%dms)",
        relay_name, config.debounce_ms,
    )

    await _listen(relay_name, db_path)
