"""Application context — singleton relay map.

Initialised once at startup by ``amain()``, then available to any module
via ``get_relay(name)`` or ``get_relays()`` without parameter threading.

    from relay_core.context import get_relay
    relay = get_relay("ibkr")
"""

from typing import TYPE_CHECKING

from shared import RelayName

if TYPE_CHECKING:
    from relay_core import BrokerRelay

_relay_map: dict[str, "BrokerRelay"] | None = None


def init_relays(relays: list["BrokerRelay"]) -> None:
    """Set the relay map. Must be called exactly once at startup."""
    global _relay_map
    if _relay_map is not None:
        raise RuntimeError("Relay context already initialised")
    _relay_map = {r.name: r for r in relays}


def get_relays() -> dict[str, "BrokerRelay"]:
    """Return the full relay map. Raises if called before init."""
    if _relay_map is None:
        raise RuntimeError(
            "Relay context not initialised — call init_relays() first"
        )
    return _relay_map


def get_relay(name: RelayName) -> "BrokerRelay":
    """Look up a single relay by name. Raises on unknown name."""
    relays = get_relays()
    try:
        return relays[name]
    except KeyError:
        raise KeyError(f"Unknown relay {name!r}") from None


def _reset() -> None:
    """Reset the context — for tests only."""
    global _relay_map
    _relay_map = None
