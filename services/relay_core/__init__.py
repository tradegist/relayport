"""Relay core — generic poller and listener engine types.

Broker adapters provide callbacks; the engines handle orchestration
(dedup, aggregate, notify, mark, reconnect, debounce).
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from notifier.base import BaseNotifier
from shared import Fill, RelayName

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
    *on_message*: parse a raw WS JSON dict into a Fill (or None to skip).
    *event_filter*: return True if the event should be processed, False to skip.
    *exec_events_enabled*: whether preliminary exec events fire webhooks.
    *debounce_ms*: milliseconds to buffer fills before flushing (0 = disabled).
    """

    ws_url: str
    api_token: str
    on_message: Callable[[dict[str, Any]], Fill | None]
    event_filter: Callable[[dict[str, Any]], bool]
    exec_events_enabled: bool = False
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
