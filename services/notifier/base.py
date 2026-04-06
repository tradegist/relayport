"""Base class for all notifier backends."""

from abc import ABC, abstractmethod

from pydantic import BaseModel


class BaseNotifier(ABC):
    """Interface all notifier backends must implement."""

    name: str

    def __init__(self, suffix: str = "") -> None:  # noqa: B027
        """Initialize the notifier. Suffix supports multi-instance env vars."""

    @staticmethod
    @abstractmethod
    def required_env_vars() -> list[str]:
        """Env vars that must be set for this notifier to function."""
        ...

    @abstractmethod
    def send(self, payload: BaseModel) -> None:
        """Deliver the notification. Log errors internally, never raise."""
        ...
