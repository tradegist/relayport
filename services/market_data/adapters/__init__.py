from abc import ABC, abstractmethod

from market_data.models.dividends import DividendsUpcomingItem

_registry: dict[str, type["MarketDataAdapter"]] = {}
_instances: dict[str, "MarketDataAdapter"] = {}


class MarketDataAdapter(ABC):
    @abstractmethod
    def get_dividends_upcoming(
        self, symbols: list[str]
    ) -> tuple[dict[str, DividendsUpcomingItem], dict[str, str]]:
        ...


def register(name: str, adapter_cls: type[MarketDataAdapter]) -> None:
    _registry[name] = adapter_cls
    _instances.pop(name, None)


def get_adapter(name: str) -> MarketDataAdapter | None:
    cls = _registry.get(name)
    if cls is None:
        return None
    cached = _instances.get(name)
    if cached is not None and type(cached) is cls:
        return cached
    instance = cls()
    _instances[name] = instance
    return instance


def known_targets() -> list[str]:
    return list(_registry.keys())
