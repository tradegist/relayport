"""Relay core — generic poller and listener engine types.

Broker adapters provide callbacks; the engines handle orchestration
(dedup, aggregate, notify, mark, reconnect, debounce).
"""

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from notifier.base import BaseNotifier
from shared import Fill, RelayName

# ── Relay-agnostic env var getters ───────────────────────────────────
# Pattern: check {RELAY}_VAR first, fall back to generic VAR.


def get_poll_interval(relay_name: RelayName) -> int:
    """Read {RELAY}_POLL_INTERVAL, falling back to POLL_INTERVAL."""
    prefix = relay_name.upper()
    raw = os.environ.get(f"{prefix}_POLL_INTERVAL", "").strip()
    if not raw:
        raw = os.environ.get("POLL_INTERVAL", "600").strip()
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(
            f"Invalid poll interval={raw!r} — must be an integer"
        ) from None


def is_poller_enabled(relay_name: RelayName) -> bool:
    """Check {RELAY}_POLLER_ENABLED, falling back to POLLER_ENABLED.

    Defaults to True (polling is on unless explicitly disabled).
    """
    prefix = relay_name.upper()
    val = os.environ.get(f"{prefix}_POLLER_ENABLED", "").strip().lower()
    if not val:
        val = os.environ.get("POLLER_ENABLED", "").strip().lower()
    if not val:
        return True
    return val not in ("0", "false", "no")


def is_listener_enabled(relay_name: RelayName) -> bool:
    """Check {RELAY}_LISTENER_ENABLED, falling back to LISTENER_ENABLED."""
    prefix = relay_name.upper()
    val = os.environ.get(f"{prefix}_LISTENER_ENABLED", "").strip().lower()
    if not val:
        val = os.environ.get("LISTENER_ENABLED", "").strip().lower()
    return val not in ("0", "false", "no", "")


def get_debounce_ms(relay_name: RelayName) -> int:
    """Read {RELAY}_LISTENER_DEBOUNCE_MS, falling back to LISTENER_DEBOUNCE_MS."""
    prefix = relay_name.upper()
    raw = os.environ.get(f"{prefix}_LISTENER_DEBOUNCE_MS", "").strip()
    if not raw:
        raw = os.environ.get("LISTENER_DEBOUNCE_MS", "0").strip()
    try:
        val = int(raw)
    except ValueError:
        raise SystemExit(
            f"Invalid debounce time={raw!r} — must be an integer"
        ) from None
    if val < 0:
        raise SystemExit(f"Invalid debounce time={val} — must be >= 0")
    return val


# Type alias for dispatch handlers passed to on_message.
# The adapter calls send_and_mark(fill) for full pipeline (dedup + notify + mark)
# or send_no_mark(fill) for fire-and-forget dispatch.
FillHandler = Callable[[Fill], Awaitable[None]]

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


# ── Listener configuration ───────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ListenerConfig:
    """Everything the generic WS listener engine needs from a broker adapter.

    *ws_url*: WebSocket endpoint to connect to.
    *api_token*: Bearer token for WS auth.
    *on_message*: async callback receiving (data, send_and_mark, send_no_mark).
        The adapter parses the raw WS JSON dict, builds a Fill, and calls
        the appropriate handler — ``send_and_mark`` for full pipeline
        (dedup + notify + mark) or ``send_no_mark`` for fire-and-forget.
    *event_filter*: return True if the event should be processed, False to skip.
    *debounce_ms*: milliseconds to buffer fills before flushing (0 = disabled).
    """

    ws_url: str
    api_token: str
    on_message: Callable[
        [dict[str, Any], FillHandler, FillHandler], Awaitable[None]
    ]
    event_filter: Callable[[dict[str, Any]], bool]
    debounce_ms: int = 0


# ── Broker relay ─────────────────────────────────────────────────────

@dataclass(slots=True)
class BrokerRelay:
    """A fully configured relay instance for one broker.

    A relay has a name, notifiers, and optionally a poller and/or listener.
    The registry instantiates one of these per entry in RELAYS.
    """

    name: RelayName
    notifiers: list[BaseNotifier]
    poller_configs: list[PollerConfig] = field(default_factory=list)
    listener_config: ListenerConfig | None = None

    # Runtime state (set by the orchestrator, not by the adapter)
    poll_locks: list[asyncio.Lock] = field(default_factory=list)
