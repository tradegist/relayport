"""Bridge→Relay real-time listener — WebSocket subscriber.

Connects to ibkr_bridge's ``GET /ibkr/ws/events`` WebSocket endpoint,
receives execution events (fills), dedups, aggregates, and dispatches
webhooks via the notifier package.

Runs as a background asyncio task inside the poller container.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import aiohttp

from dedup import get_processed_ids, mark_processed_batch
from dedup import init_db as _init_dedup_db
from notifier import notify
from notifier.base import BaseNotifier
from shared import (
    DEDUP_DB_PATH,
    BuySell,
    Fill,
    Source,
    WebhookPayloadTrades,
    aggregate_fills,
    normalize_asset_class,
)

from .bridge_models import WsEnvelope

log = logging.getLogger("listener")

# ── Config ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ListenerConfig:
    """Validated listener configuration (built eagerly in main)."""

    ws_url: str
    api_token: str
    exec_events_enabled: bool
    debounce_ms: int
    db_path: str


# ── Reconnection constants ───────────────────────────────────────────
_INITIAL_RETRY_DELAY = 5
_MAX_RETRY_DELAY = 300
_RETRY_BACKOFF_FACTOR = 2

# ── Side mapping (financial enum — never assume a default) ───────────
_SIDE_MAP: dict[str, BuySell] = {
    "BOT": BuySell.BUY,
    "SLD": BuySell.SELL,
}


# ── Env var getters ──────────────────────────────────────────────────

def get_bridge_ws_url() -> str:
    """Return BRIDGE_WS_URL (required when listener is enabled)."""
    val = os.environ.get("BRIDGE_WS_URL", "").strip()
    if not val:
        raise SystemExit("BRIDGE_WS_URL must be set when LISTENER_ENABLED is set")
    return val


def get_bridge_api_token() -> str:
    """Return BRIDGE_API_TOKEN (required when listener is enabled)."""
    val = os.environ.get("BRIDGE_API_TOKEN", "").strip()
    if not val:
        raise SystemExit("BRIDGE_API_TOKEN must be set when LISTENER_ENABLED is set")
    return val


def is_listener_enabled() -> bool:
    """Return True if LISTENER_ENABLED is set to a truthy value."""
    val = os.environ.get("LISTENER_ENABLED", "").strip().lower()
    return val not in ("0", "false", "no", "")


def is_exec_events_enabled() -> bool:
    """Return True if LISTENER_EXEC_EVENTS_ENABLED is truthy."""
    val = os.environ.get("LISTENER_EXEC_EVENTS_ENABLED", "false").strip().lower()
    return val not in ("0", "false", "no", "")


def get_debounce_ms() -> int:
    """Return LISTENER_EVENT_DEBOUNCE_TIME in milliseconds (>= 0)."""
    raw = os.environ.get("LISTENER_EVENT_DEBOUNCE_TIME", "0").strip()
    try:
        val = int(raw)
    except ValueError:
        raise SystemExit(
            f"Invalid LISTENER_EVENT_DEBOUNCE_TIME={raw!r} — must be an integer"
        ) from None
    if val < 0:
        raise SystemExit(
            f"Invalid LISTENER_EVENT_DEBOUNCE_TIME={val} — must be >= 0"
        )
    return val


# ── Fill mapping ─────────────────────────────────────────────────────

def map_fill(envelope: WsEnvelope) -> Fill | None:
    """Map a WsEnvelope with fill data to a relay Fill model.

    Returns None and logs an error if:
    - The envelope has no fill data (status events).
    - The execution side is not ``"BOT"`` or ``"SLD"``.
    """
    if envelope.fill is None:
        log.error(
            "Envelope seq=%d type=%s has no fill data",
            envelope.seq, envelope.type,
        )
        return None

    ex = envelope.fill.execution
    contract = envelope.fill.contract
    cr = envelope.fill.commissionReport

    exec_id = ex.execId.strip()
    if not exec_id:
        log.error(
            "Empty execId for envelope seq=%d type=%s symbol=%s — skipping fill",
            envelope.seq, envelope.type, contract.symbol,
        )
        return None

    # Financial enum — never assume a default for buy/sell side.
    side = _SIDE_MAP.get(ex.side)
    if side is None:
        log.error(
            "Unknown execution side %r for execId=%s — skipping fill",
            ex.side, exec_id,
        )
        return None

    source = cast(Source, envelope.type)

    return Fill(
        execId=exec_id,
        orderId=str(ex.permId),
        symbol=contract.symbol,
        assetClass=normalize_asset_class(contract.secType),
        side=side,
        orderType=None,  # WS events don't carry order type info
        price=ex.price,
        volume=ex.shares,
        cost=ex.price * ex.shares,
        fee=abs(cr.commission),  # Always positive (amount paid)
        timestamp=ex.time,
        source=source,
        raw=envelope.model_dump(),
    )


# ── Dispatch helpers (blocking IO — run in asyncio.to_thread) ────────

def _send_and_mark(
    fills: list[Fill],
    notifiers: list[BaseNotifier],
    db_path: str,
) -> None:
    """Dedup, aggregate, notify, and mark fills as processed.

    All blocking IO (SQLite + HTTP webhooks) in one function.
    Creates a thread-local SQLite connection (never shares across threads).
    """
    conn = _init_dedup_db(Path(db_path))
    try:
        candidate_ids = {f.execId for f in fills}
        already_seen = get_processed_ids(conn, candidate_ids)
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

        for trade in trades:
            log.info(
                "Listener trade: %s %s orderId=%s @ %s (vol %s, %d fill(s))",
                trade.side.value, trade.symbol, trade.orderId,
                trade.price, trade.volume, trade.fillCount,
            )

        # Notify then mark (mark-after-notify pattern).
        # WARNING: notify() currently swallows per-backend exceptions
        # without raising, so mark_processed_batch() always executes —
        # even when all notifiers fail.  Since the listener shares the
        # dedup DB with the Flex poller, those execIds are permanently
        # suppressed (poller dedup-skips them on the next cycle).
        # See docs/notifier-resilience.md for the planned fix:
        # notify() will raise NotificationError when ALL backends fail,
        # preventing mark and allowing retry on the next flush.
        payload = WebhookPayloadTrades(relay="ibkr", data=trades, errors=[])
        notify(notifiers, payload)

        # Mark processed AFTER notify
        all_new_ids = [eid for t in trades for eid in t.execIds]
        mark_processed_batch(conn, all_new_ids)
        log.info("Marked %d fill(s) as processed", len(all_new_ids))
    finally:
        conn.close()


def _send_no_mark(fills: list[Fill], notifiers: list[BaseNotifier]) -> None:
    """Aggregate and notify WITHOUT dedup or marking.

    Used for execDetailsEvent (preliminary fills). These are fire-and-forget;
    the commissionReportEvent handles dedup and marking later.
    """
    trades = aggregate_fills(fills)
    if not trades:
        return

    for trade in trades:
        log.info(
            "Listener preliminary: %s %s orderId=%s @ %s (no commission)",
            trade.side.value, trade.symbol, trade.orderId, trade.price,
        )
    notify(notifiers, WebhookPayloadTrades(relay="ibkr", data=trades, errors=[]))


# ── Debounce buffer ──────────────────────────────────────────────────

class _DebounceBuffer:
    """Buffer commissionReportEvent fills and flush after a quiet window."""

    def __init__(
        self,
        debounce_ms: int,
        notifiers: list[BaseNotifier],
        db_path: str,
    ) -> None:
        self._debounce_s = debounce_ms / 1000.0
        self._notifiers = notifiers
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
                _send_and_mark, fills, self._notifiers, self._db_path,
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


# ── WebSocket event handler ──────────────────────────────────────────

async def _handle_event(
    data: dict[str, Any],
    notifiers: list[BaseNotifier],
    exec_events_enabled: bool,
    debounce_buf: _DebounceBuffer | None,
    db_path: str,
) -> None:
    """Process a single parsed WS message."""
    event_type = data.get("type")

    # Status events — log only
    if event_type in ("connected", "disconnected"):
        log.info("Bridge status: %s", event_type)
        return

    if event_type not in ("execDetailsEvent", "commissionReportEvent"):
        log.warning("Unrecognized event type: %s", event_type)
        return

    # Validate with Pydantic
    try:
        envelope = WsEnvelope.model_validate(data)
    except Exception:
        log.exception("Failed to validate WsEnvelope (type=%s)", event_type)
        return

    # execDetailsEvent — dispatch only if enabled (preliminary, no dedup/mark).
    # These are fire-and-forget; the commissionReportEvent handles dedup later.
    if envelope.type == "execDetailsEvent":
        if not exec_events_enabled:
            log.debug("Skipping execDetailsEvent (disabled)")
            return
        fill = map_fill(envelope)
        if fill is None:
            return
        log.info(
            "Preliminary fill: %s %s execId=%s",
            fill.side.value, fill.symbol, fill.execId,
        )
        try:
            await asyncio.to_thread(_send_no_mark, [fill], notifiers)
        except Exception:
            log.exception(
                "Failed to dispatch exec event execId=%s", fill.execId,
            )
        return

    # commissionReportEvent — full pipeline (dedup + mark)
    fill = map_fill(envelope)
    if fill is None:
        return

    log.info(
        "Commission fill: %s %s execId=%s fee=%s",
        fill.side.value, fill.symbol, fill.execId, fill.fee,
    )

    if debounce_buf is not None:
        await debounce_buf.add(fill)
    else:
        try:
            await asyncio.to_thread(
                _send_and_mark, [fill], notifiers, db_path,
            )
        except Exception:
            log.exception(
                "Failed to dispatch commission event execId=%s", fill.execId,
            )


# ── WebSocket listener loop ──────────────────────────────────────────

async def _listen(
    ws_url: str,
    api_token: str,
    notifiers: list[BaseNotifier],
    exec_events_enabled: bool,
    debounce_ms: int,
    db_path: str,
) -> None:
    """Connect, process events, reconnect with exponential backoff."""
    last_seq = 0
    retry_delay = _INITIAL_RETRY_DELAY

    debounce_buf: _DebounceBuffer | None = None
    if debounce_ms > 0:
        debounce_buf = _DebounceBuffer(debounce_ms, notifiers, db_path)

    while True:
        url = ws_url
        if last_seq > 0:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}last_seq={last_seq}"

        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {api_token}"}
                log.info("Connecting to bridge WS (last_seq=%d)", last_seq)
                log.debug("Bridge WS URL: %s", url)

                async with session.ws_connect(
                    url, headers=headers, heartbeat=30.0,
                ) as ws:
                    log.info(
                        "Connected to bridge WS (last_seq=%d)", last_seq,
                    )
                    retry_delay = _INITIAL_RETRY_DELAY

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                event_data = json.loads(msg.data)
                            except json.JSONDecodeError:
                                log.error(
                                    "Failed to parse WS message: %.200s",
                                    msg.data,
                                )
                                continue

                            seq = event_data.get("seq")
                            if isinstance(seq, int):
                                last_seq = seq

                            await _handle_event(
                                event_data,
                                notifiers,
                                exec_events_enabled,
                                debounce_buf,
                                db_path,
                            )
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            log.error("WS error: %s", ws.exception())
                            break
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                        ):
                            log.warning(
                                "WS closed by server (code=%s)", msg.data,
                            )
                            break

        except aiohttp.ClientError as exc:
            log.error("WS connection error: %s", exc)
        except asyncio.CancelledError:
            log.info("Listener cancelled — shutting down")
            if debounce_buf is not None:
                await debounce_buf.flush()
            raise
        except Exception:
            log.exception("Unexpected error in listener")

        # Flush buffered fills before reconnect
        if debounce_buf is not None:
            try:
                await debounce_buf.flush()
            except Exception:
                log.exception(
                    "Failed to flush debounce buffer on disconnect",
                )

        log.info("Reconnecting in %ds...", retry_delay)
        await asyncio.sleep(retry_delay)
        retry_delay = min(
            retry_delay * _RETRY_BACKOFF_FACTOR, _MAX_RETRY_DELAY,
        )


# ── Public API ───────────────────────────────────────────────────────

def build_listener_config() -> ListenerConfig:
    """Build and validate listener config from env vars.

    Call on the main coroutine so SystemExit actually kills the process
    (SystemExit inside an asyncio task is silently swallowed).
    """
    return ListenerConfig(
        ws_url=get_bridge_ws_url(),
        api_token=get_bridge_api_token(),
        exec_events_enabled=is_exec_events_enabled(),
        debounce_ms=get_debounce_ms(),
        db_path=DEDUP_DB_PATH,
    )


async def start_listener(
    cfg: ListenerConfig,
    notifiers: list[BaseNotifier],
) -> None:
    """Start the WebSocket listener (runs indefinitely with auto-reconnect)."""
    log.info(
        "Listener starting (exec_events=%s, debounce=%dms)",
        cfg.exec_events_enabled, cfg.debounce_ms,
    )
    log.debug("Bridge WS URL: %s", cfg.ws_url)

    await _listen(
        ws_url=cfg.ws_url,
        api_token=cfg.api_token,
        notifiers=notifiers,
        exec_events_enabled=cfg.exec_events_enabled,
        debounce_ms=cfg.debounce_ms,
        db_path=cfg.db_path,
    )
