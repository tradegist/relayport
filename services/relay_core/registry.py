"""Relay registry — load configured relays from the RELAYS env var.

Reads ``RELAYS`` (comma-separated relay names, e.g. ``ibkr``),
validates each against ``RelayName``, dynamically imports the adapter
module, and calls ``build_relay()`` to produce ``BrokerRelay`` instances.
"""

import importlib
import logging
import os
from typing import get_args

from notifier import load_notifiers
from notifier.base import BaseNotifier
from shared import RelayName

from . import BrokerRelay

log = logging.getLogger("relay_registry")

# Build the valid set from the Literal type at runtime.
_VALID_RELAY_NAMES: set[str] = set(get_args(RelayName))


def get_relay_names() -> list[RelayName]:
    """Parse and validate the RELAYS env var.

    Returns:
        List of validated relay names (empty if RELAYS is unset/blank).

    Raises:
        SystemExit: If RELAYS contains an unknown relay name.
    """
    raw = os.environ.get("RELAYS", "").strip()
    if not raw:
        return []

    names: list[RelayName] = []
    for name in raw.split(","):
        name = name.strip().lower()
        if not name:
            continue
        if name not in _VALID_RELAY_NAMES:
            raise SystemExit(
                f"Unknown relay {name!r} in RELAYS. "
                f"Valid: {', '.join(sorted(_VALID_RELAY_NAMES))}"
            )
        # Safe to cast — validated against _VALID_RELAY_NAMES.
        names.append(name)  # type: ignore[arg-type]

    return names


def _load_adapter(
    relay_name: RelayName, notifiers: list[BaseNotifier],
) -> BrokerRelay:
    """Import a relay adapter and call its build_relay().

    Each adapter lives at ``relays.<name>`` and exports a
    ``build_relay(notifiers) -> BrokerRelay`` function.
    """
    module_path = f"relays.{relay_name}"
    try:
        mod = importlib.import_module(module_path)
    except ImportError as exc:
        raise SystemExit(
            f"Failed to import relay adapter {module_path!r}: {exc}"
        ) from exc

    build_fn = getattr(mod, "build_relay", None)
    if build_fn is None:
        raise SystemExit(
            f"Relay adapter {module_path!r} does not export build_relay()"
        )

    relay: BrokerRelay = build_fn(notifiers)
    return relay


def load_relays() -> list[BrokerRelay]:
    """Load all configured relays from RELAYS env var.

    Returns:
        List of fully configured BrokerRelay instances, ready to start.

    Raises:
        SystemExit: On invalid RELAYS value or adapter import failure.
    """
    names = get_relay_names()
    notifiers: list[BaseNotifier] = load_notifiers()
    relays: list[BrokerRelay] = []

    for name in names:
        log.info("Loading relay: %s", name)
        relay = _load_adapter(name, notifiers)
        relays.append(relay)
        log.info(
            "Relay %s: %d poller(s), listener=%s",
            name,
            len(relay.poller_configs),
            "enabled" if relay.listener_config else "disabled",
        )

    return relays
