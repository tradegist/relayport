"""Notifier registry — load, validate, and dispatch to configured backends."""

import logging
import os

from pydantic import BaseModel

from .base import BaseNotifier
from .webhook import WebhookNotifier

log = logging.getLogger("notifier")

REGISTRY: dict[str, type[BaseNotifier]] = {
    "webhook": WebhookNotifier,
}


def load_notifiers(suffix: str = "") -> list[BaseNotifier]:
    """Read NOTIFIERS env var, validate config, return instantiated backends.

    Args:
        suffix: Env var suffix for multi-instance support (e.g. "_2").
                Applied to both NOTIFIERS and each backend's required vars.

    Returns:
        List of ready-to-use notifier instances. Empty list = dry-run mode.

    Raises:
        SystemExit: If a notifier name is unknown or required env vars are missing.
    """
    raw = os.environ.get(f"NOTIFIERS{suffix}", "").strip()
    if not raw:
        log.info("No notifiers configured (NOTIFIERS%s is empty) — dry-run mode", suffix)
        return []

    names = [n.strip() for n in raw.split(",") if n.strip()]
    notifiers: list[BaseNotifier] = []

    for name in names:
        cls = REGISTRY.get(name)
        if cls is None:
            log.error(
                "Unknown notifier %r in NOTIFIERS%s. Available: %s",
                name, suffix, ", ".join(REGISTRY),
            )
            raise SystemExit(1)

        # Validate required env vars (with suffix)
        missing = [
            f"{var}{suffix}"
            for var in cls.required_env_vars()
            if not os.environ.get(f"{var}{suffix}")
        ]
        if missing:
            log.error(
                "Notifier %r requires env vars: %s",
                name, ", ".join(missing),
            )
            raise SystemExit(1)

        notifiers.append(cls(suffix=suffix))
        log.info("Loaded notifier: %s%s", name, suffix or "")

    return notifiers


def validate_notifier_env(suffix: str = "") -> bool:
    """Check whether NOTIFIERS env vars are valid without instantiating.

    Returns True if NOTIFIERS is set and all required vars are present.
    Returns False if NOTIFIERS is empty (no notifiers configured).
    Calls die() if partially configured (notifier named but missing vars).

    Designed for CLI pre-deploy validation (cli/_pre_sync_hook).
    """
    raw = os.environ.get(f"NOTIFIERS{suffix}", "").strip()
    if not raw:
        return False

    names = [n.strip() for n in raw.split(",") if n.strip()]

    for name in names:
        cls = REGISTRY.get(name)
        if cls is None:
            return False  # unknown notifier — let runtime error handle it

        missing = [
            f"{var}{suffix}"
            for var in cls.required_env_vars()
            if not os.environ.get(f"{var}{suffix}")
        ]
        if missing:
            from cli.core import die
            die(f"Notifier {name!r} partially configured. Missing: {', '.join(missing)}")

    return True


def notify(notifiers: list[BaseNotifier], payload: BaseModel) -> None:
    """Dispatch payload to all configured notifiers."""
    if not notifiers:
        log.info("No notifiers configured — skipping notification")
        return

    for notifier in notifiers:
        notifier.send(payload)
