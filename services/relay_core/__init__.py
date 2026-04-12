"""Relay core — generic poller and listener engine types.

Broker adapters provide callbacks; the engines handle orchestration
(dedup, aggregate, notify, mark, reconnect, debounce).
"""

import asyncio
from dataclasses import dataclass, field

from relay_core.notifier.base import BaseNotifier as BaseNotifier
from shared import RelayName

# Re-export domain types so consumers can do ``from relay_core import X``.
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

    # Runtime state (set by the orchestrator, not by the adapter)
    poll_locks: list[asyncio.Lock] = field(default_factory=list)
