from abc import ABC, abstractmethod

from market_data.models.dividends import DividendsUpcomingItem

_registry: dict[str, type["MarketDataAdapter"]] = {}


class MarketDataAdapter(ABC):
    @abstractmethod
    def get_dividends_upcoming(
        self, symbols: list[str]
    ) -> tuple[dict[str, DividendsUpcomingItem], dict[str, str]]:
        ...


def register(name: str, adapter_cls: type[MarketDataAdapter]) -> None:
    _registry[name] = adapter_cls


def get_adapter(name: str) -> MarketDataAdapter | None:
    cls = _registry.get(name)
    return cls() if cls is not None else None


def known_targets() -> list[str]:
    return list(_registry.keys())
