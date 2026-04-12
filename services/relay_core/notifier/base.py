"""Base class for all notifier backends."""

import logging
from abc import ABC, abstractmethod

from pydantic import BaseModel

from relay_core.env import get_env

log = logging.getLogger("notifier.base")


class BaseNotifier(ABC):
    """Interface all notifier backends must implement.

    Subclasses MUST validate their own configuration in ``__init__``.
    The default implementation checks that every var from ``required_env_vars()``
    is present as ``{var}{suffix}`` in the environment, logs an actionable
    error, and raises ``SystemExit`` with that descriptive message on any
    missing var.  Callers may surface the message directly (for example, from
    ``validate_notifier_env()``).  Subclasses that need custom validation
    logic (e.g. skipping a var when a fallback is available) should override
    ``__init__`` entirely.
    """

    name: str

    def __init__(self, prefix: str = "", suffix: str = "") -> None:
        """Validate required env vars and initialize.

        Args:
            prefix: Relay-specific prefix (e.g. ``"IBKR_"``).  Each var is
                    tried as ``{prefix}{var}{suffix}`` first, falling back
                    to ``{var}{suffix}`` when the prefixed version is unset.
            suffix: Multi-instance suffix (e.g. ``"_2"``).
        """
        missing: list[str] = []
        for var in self.required_env_vars():
            if not get_env(var, prefix, suffix):
                # Show the prefixed name when a prefix is active
                missing.append(f"{prefix}{var}{suffix}" if prefix else f"{var}{suffix}")
        if missing:
            msg = f"Notifier {self.name!r} requires env vars: {', '.join(missing)}"
            log.error("%s", msg)
            raise SystemExit(msg)

    @staticmethod
    @abstractmethod
    def required_env_vars() -> list[str]:
        """Env vars that must be set for this notifier to function."""
        ...

    @abstractmethod
    def send(self, payload: BaseModel) -> None:
        """Deliver the notification. Raise on failure so callers can retry."""
        ...
