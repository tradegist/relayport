"""Generic WS listener engine — broker-agnostic event loop.

The engine receives callbacks (``on_message``, ``event_filter``) via
``ListenerConfig`` and handles all orchestration: WebSocket connection,
reconnect with exponential backoff, debounce buffering, dedup, aggregate,
mark-after-notify.  Zero broker knowledge.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from relay_core.context import get_relay
from relay_core.dedup import get_processed_ids, mark_processed_batch
from relay_core.dedup import init_db as _init_dedup_db
from relay_core.env import get_env, get_env_int
from relay_core.fx import enrich_if_enabled
from relay_core.notifier import notify
from relay_core.notifier.models import WebhookPayloadTrades
from shared import Fill, RelayName, aggregate_fills

log = logging.getLogger(__name__)


# ── On-message result ────────────────────────────────────────────────


class FatalListenerError(Exception):
    """Raised when a listener encounters an unrecoverable error (e.g. bad credentials).

    The listener loop will stop retrying and shut down when this is raised.
    """


@dataclass(frozen=True, slots=True)
class OnMessageResult:
    """Return type for ListenerConfig.on_message.

    *fill*: the parsed Fill, or None to skip the event.
    *mark*: if True, use full dedup+notify+mark pipeline;
            if False, fire-and-forget (no dedup, no mark).
    """

    fill: Fill | None = None
    mark: bool = True


# ── Listener configuration ───────────────────────────────────────────


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

# ── Reconnection constants ───────────────────────────────────────────
INITIAL_RETRY_DELAY = 5
MAX_RETRY_DELAY = 300
RETRY_BACKOFF_FACTOR = 2


# ── Namespace helpers (mirror poller_engine pattern) ─────────────────

def _prefix_ids(relay_name: str, fills: list[Fill]) -> set[str]:
    """Build relay-prefixed exec IDs from a list of fills."""
    return {f"{relay_name}:{f.execId}" for f in fills}


def _strip_prefix(relay_name: str, prefixed_ids: set[str]) -> set[str]:
    """Remove relay prefix to recover original exec IDs."""
    prefix = f"{relay_name}:"
    return {pid[len(prefix):] for pid in prefixed_ids}


# ── Dispatch helpers (blocking IO — run in asyncio.to_thread) ────────

def _send_and_mark(
    relay_name: RelayName,
    fills: list[Fill],
    db_path: str | None,
) -> None:
    """Dedup, aggregate, notify, and mark fills as processed.

    All blocking IO (SQLite + HTTP webhooks) in one function.
    Creates a thread-local SQLite connection (never shares across threads).
    Resolves notifiers and retry config from the relay context.

    If all notifiers fail, ``NotificationError`` propagates — fills stay
    unprocessed and will be retried on the next event or reconnect.
    """
    relay = get_relay(relay_name)
    conn = _init_dedup_db(db_path)
    try:
        prefixed_candidates = _prefix_ids(relay_name, fills)
        already_seen_prefixed = get_processed_ids(conn, prefixed_candidates)
        already_seen = _strip_prefix(relay_name, already_seen_prefixed)
        new_fills = [f for f in fills if f.execId not in already_seen]

        if not new_fills:
            log.debug("All %d fill(s) already processed", len(fills))
            return

        log.info(
            "%d new fill(s) after dedup (of %d received)",
            len(new_fills), len(fills),
        )

        trades = aggregate_fills(new_fills)
        if not trades:
            return

        fx_errors: list[str] = []
        trades = enrich_if_enabled(trades, fx_errors)

        for trade in trades:
            log.info(
                "Listener trade: %s %s orderId=%s @ %s (vol %s, %d fill(s))",
                trade.side.value, trade.symbol, trade.orderId,
                trade.price, trade.volume, trade.fillCount,
            )

        # Mark-after-notify: notify then mark (never reversed).
        # If notify raises NotificationError, mark is skipped.
        payload = WebhookPayloadTrades(relay=relay_name, data=trades, errors=fx_errors)
        notify(
            relay.notifiers, payload,
            retries=relay.notify_retries,
            retry_delay_ms=relay.notify_retry_delay_ms,
        )

        # Mark processed AFTER notify (relay-prefixed keys)
        prefixed_new = [f"{relay_name}:{eid}" for t in trades for eid in t.execIds]
        mark_processed_batch(conn, prefixed_new)
        log.info("Marked %d fill(s) as processed", len(prefixed_new))
    finally:
        conn.close()


def _send_no_mark(
    relay_name: RelayName,
    fills: list[Fill],
) -> None:
    """Aggregate and notify WITHOUT dedup or marking.

    Used for preliminary exec events (fire-and-forget).
    Resolves notifiers and retry config from the relay context.
    """
    relay = get_relay(relay_name)
    trades = aggregate_fills(fills)
    if not trades:
        return

    fx_errors: list[str] = []
    trades = enrich_if_enabled(trades, fx_errors)

    for trade in trades:
        log.info(
            "Listener preliminary: %s %s orderId=%s @ %s (no commission)",
            trade.side.value, trade.symbol, trade.orderId, trade.price,
        )
    notify(
        relay.notifiers,
        WebhookPayloadTrades(relay=relay_name, data=trades, errors=fx_errors),
        retries=relay.notify_retries,
        retry_delay_ms=relay.notify_retry_delay_ms,
    )


# ── Debounce buffer ──────────────────────────────────────────────────

class DebounceBuffer:
    """Buffer fills and flush after a quiet window.

    Public so adapters can reference the type, but created internally
    by ``start_listener``.
    """

    def __init__(
        self,
        relay_name: RelayName,
        debounce_ms: int,
        db_path: str | None,
    ) -> None:
        self._relay_name = relay_name
        self._debounce_s = debounce_ms / 1000.0
        self._db_path = db_path
        self._buffer: list[Fill] = []
        self._flush_task: asyncio.Task[None] | None = None
        self._flushing = False

    async def add(self, fill: Fill) -> None:
        """Add a fill and (re)start the debounce timer."""
        self._buffer.append(fill)
        # Only cancel the pending sleep — never cancel an in-progress flush.
        if (
            self._flush_task is not None
            and not self._flush_task.done()
            and not self._flushing
        ):
            self._flush_task.cancel()
        self._flush_task = asyncio.create_task(self._delayed_flush())

    async def _delayed_flush(self) -> None:
        await asyncio.sleep(self._debounce_s)
        await self.flush()

    async def flush(self) -> None:
        """Flush all buffered fills — safe to call even if empty."""
        if not self._buffer:
            return
        self._flushing = True
        fills = self._buffer.copy()
        self._buffer.clear()
        try:
            await asyncio.to_thread(
                _send_and_mark, self._relay_name, fills,
                self._db_path,
            )
        except asyncio.CancelledError:
            log.warning(
                "Flush cancelled — restoring %d fill(s) to buffer", len(fills),
            )
            self._buffer = fills + self._buffer
            raise
        except Exception:
            log.exception("Failed to dispatch %d buffered fill(s)", len(fills))
            # Re-add to front so they are retried on next flush
            self._buffer = fills + self._buffer
        finally:
            self._flushing = False


# ── Event handler ────────────────────────────────────────────────────

async def _handle_event(
    relay_name: RelayName,
    data: Any,
    debounce_buf: DebounceBuffer | None,
    db_path: str | None,
) -> None:
    """Process a single parsed WS message using adapter callbacks."""
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

    mark_fills: list[Fill] = []
    no_mark_fills: list[Fill] = []

    for result in results:
        if result.fill is None:
            continue
        fill = result.fill
        if result.mark:
            log.info(
                "[%s] Fill: %s %s execId=%s fee=%s",
                relay_name, fill.side.value, fill.symbol, fill.execId, fill.fee,
            )
            mark_fills.append(fill)
        else:
            log.info(
                "[%s] Fill (no-mark): %s %s execId=%s",
                relay_name, fill.side.value, fill.symbol, fill.execId,
            )
            no_mark_fills.append(fill)

    if mark_fills:
        if debounce_buf is not None:
            for fill in mark_fills:
                await debounce_buf.add(fill)
        else:
            try:
                await asyncio.to_thread(
                    _send_and_mark, relay_name, mark_fills, db_path,
                )
            except Exception:
                log.exception(
                    "[%s] Failed to dispatch %d fill(s)",
                    relay_name, len(mark_fills),
                )

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

    debounce_buf: DebounceBuffer | None = None
    if config.debounce_ms > 0:
        debounce_buf = DebounceBuffer(
            relay_name, config.debounce_ms, db_path,
        )

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

                            await _handle_event(
                                relay_name, event_data,
                                debounce_buf, db_path,
                            )
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            log.error("[%s] WS error: %s", relay_name, ws.exception())
                            break
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                        ):
                            log.warning(
                                "[%s] WS closed by server (code=%s)",
                                relay_name, msg.data,
                            )
                            break
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
            if debounce_buf is not None:
                await debounce_buf.flush()
            raise
        except Exception:
            log.exception("[%s] Unexpected error in listener", relay_name)

        # Flush buffered fills before reconnect
        if debounce_buf is not None:
            try:
                await debounce_buf.flush()
            except Exception:
                log.exception(
                    "[%s] Failed to flush debounce buffer on disconnect",
                    relay_name,
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
