"""Relay core — generic poller and listener engine types.

Broker adapters provide callbacks; the engines handle orchestration
(dedup, aggregate, notify, mark, reconnect, debounce).
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from relay_core.notifier.base import BaseNotifier as BaseNotifier
from shared import RelayName

# Re-export domain types so consumers can do ``from relay_core import X``.
from .listener_engine import (
    FatalListenerError as FatalListenerError,
)
from .listener_engine import (
    ListenerConfig as ListenerConfig,
)
from .listener_engine import (
    OnMessageResult as OnMessageResult,
)
from .listener_engine import (
    get_debounce_ms as get_debounce_ms,
)
from .listener_engine import (
    is_listener_enabled as is_listener_enabled,
)
from .parsing import require_float as require_float
from .parsing import require_str as require_str
from .poller_engine import (
    PollerConfig as PollerConfig,
)
from .poller_engine import (
    get_poll_interval as get_poll_interval,
)
from .poller_engine import (
    is_poller_enabled as is_poller_enabled,
)

# ── Broker relay ─────────────────────────────────────────────────────

@dataclass(slots=True)
class BrokerRelay:
    """A fully configured relay instance for one broker.

    A relay has a name, notifiers, and optionally a poller and/or listener.
    The registry instantiates one of these per entry in RELAYS.
    """

    name: RelayName
    notifiers: list[BaseNotifier]
    notify_retries: int = 0
    notify_retry_delay_ms: int = 1000
    poller_configs: list[PollerConfig] = field(default_factory=list)
    listener_config: ListenerConfig | None = None

    # Lifecycle hook — called by the orchestrator before the event loop starts.
    # Relay adapters use this to register cross-cutting concerns (e.g. log filters).
    on_start: Callable[["StartupContext"], None] | None = None

    # Runtime state (set by the orchestrator, not by the adapter)
    poll_locks: list[asyncio.Lock] = field(default_factory=list)


# ── Relay startup lifecycle ───────────────────────────────────────────


class StartupContext:
    """Passed to each relay's on_start() during orchestrator startup.

    Relays use this to register cross-cutting concerns without depending
    on the orchestrator directly.  Currently supports logging filters;
    further hooks can be added here as needed.
    """

    def __init__(self) -> None:
        self._log_filters: list[logging.Filter] = []

    def add_logging_filter(self, f: logging.Filter) -> None:
        """Register a filter to be applied to every root logging handler."""
        self._log_filters.append(f)

    def apply(self) -> None:
        """Apply all registered filters to the root logger's handlers.

        Called once by the orchestrator after all relays have started.
        """
        for handler in logging.getLogger().handlers:
            for f in self._log_filters:
                handler.addFilter(f)
