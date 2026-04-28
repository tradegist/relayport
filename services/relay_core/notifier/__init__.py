"""Notifier registry — load, validate, and dispatch to configured backends."""

import logging
import time
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel

from relay_core.alerter import send_alert
from relay_core.env import get_env, get_env_int

from .base import BaseNotifier
from .webhook import WebhookNotifier

log = logging.getLogger("notifier")


class NotificationError(Exception):
    """Raised when ALL notifier backends fail to deliver."""

    def __init__(self, failures: list[tuple[str, Exception]]) -> None:
        self.failures = failures
        names = ", ".join(name for name, _ in failures)
        super().__init__(f"All notifiers failed: {names}")

REGISTRY: dict[str, type[BaseNotifier]] = {
    "webhook": WebhookNotifier,
}


def _get_notifiers_config(prefix: str, suffix: str) -> str:
    """Read ``{prefix}NOTIFIERS{suffix}``, falling back to ``NOTIFIERS{suffix}``."""
    return get_env("NOTIFIERS", prefix, suffix)


def load_notifiers(prefix: str = "", suffix: str = "") -> list[BaseNotifier]:
    """Read NOTIFIERS env var, instantiate backends, return ready list.

    Each backend validates its own configuration in ``__init__``.

    Args:
        prefix: Relay-specific prefix (e.g. ``"IBKR_"``).  Each var is
                tried as ``{prefix}{var}{suffix}`` first, falling back
                to ``{var}{suffix}`` when the prefixed version is unset.
        suffix: Env var suffix for multi-instance support (e.g. "_2").

    Returns:
        List of ready-to-use notifier instances. Empty list = dry-run mode.

    Raises:
        SystemExit: If a notifier name is unknown or a backend rejects its config.
    """
    label = f"{prefix}NOTIFIERS{suffix}" if prefix else f"NOTIFIERS{suffix}"
    raw = _get_notifiers_config(prefix, suffix)
    if not raw:
        log.info("No notifiers configured (%s is empty) — dry-run mode", label)
        _warn_orphaned_notifier_vars(prefix, suffix)
        return []

    names = [n.strip() for n in raw.split(",") if n.strip()]
    notifiers: list[BaseNotifier] = []

    for name in names:
        cls = REGISTRY.get(name)
        if cls is None:
            msg = (
                f"Unknown notifier {name!r} in {label}. "
                f"Available: {', '.join(REGISTRY)}"
            )
            log.error("%s", msg)
            raise SystemExit(msg)

        notifiers.append(cls(prefix=prefix, suffix=suffix))
        log.info("Loaded notifier: %s (prefix=%s, suffix=%s)", name, prefix or "-", suffix or "-")

    return notifiers


def _warn_orphaned_notifier_vars(prefix: str = "", suffix: str = "") -> None:
    """Warn if any registered notifier's env vars are set but NOTIFIERS is empty."""
    label = f"{prefix}NOTIFIERS{suffix}" if prefix else f"NOTIFIERS{suffix}"
    for name, cls in REGISTRY.items():
        orphaned: list[str] = []
        for var in cls.required_env_vars():
            if get_env(var, prefix, suffix):
                orphaned.append(f"{prefix}{var}{suffix}" if prefix else f"{var}{suffix}")
        if orphaned:
            log.warning(
                "%s is empty but %s env vars are set: %s. "
                "Add %s=%s to enable delivery, "
                "or remove them to silence this warning.",
                label, name, ", ".join(orphaned), label, name,
            )


def validate_notifier_env(prefix: str = "", suffix: str = "") -> bool:
    """Check whether NOTIFIERS env vars are valid by instantiating backends.

    Returns True if NOTIFIERS is set and all backends accept their config.
    Returns False if NOTIFIERS is empty (no notifiers configured).
    Calls die() if a backend rejects its config (missing env vars).

    Designed for CLI pre-deploy validation (cli/_pre_sync_hook).
    """
    raw = _get_notifiers_config(prefix, suffix)
    if not raw:
        # Warn if notifier env vars are set but NOTIFIERS is empty —
        # likely a misconfiguration after the notifier migration.
        _warn_orphaned_notifier_vars(prefix, suffix)
        return False

    names = [n.strip() for n in raw.split(",") if n.strip()]

    for name in names:
        cls = REGISTRY.get(name)
        if cls is None:
            return False  # unknown notifier — let runtime error handle it

        try:
            cls(prefix=prefix, suffix=suffix)
        except SystemExit as exc:
            from cli.core import die  # lazy: cli/ not available in Docker containers
            detail = str(exc) if str(exc) else f"Notifier {name!r} partially configured"
            die(f"{detail} — check env vars")

    return True


def load_retry_config(prefix: str = "", suffix: str = "") -> tuple[int, int]:
    """Read NOTIFY_RETRIES and NOTIFY_RETRY_DELAY_MS from env.

    Supports relay-specific prefix (e.g. ``IBKR_NOTIFY_RETRIES``) with
    fallback to the generic var, and multi-instance suffix (e.g. ``_2``).

    Returns:
        (retries, retry_delay_ms) tuple.

    Raises:
        SystemExit: On invalid values or out-of-range.
    """
    name_r, retries = get_env_int("NOTIFY_RETRIES", prefix, suffix, "0")
    if retries < 0 or retries > 5:
        raise SystemExit(f"Invalid {name_r}={retries} — must be 0-5")

    name_d, delay_ms = get_env_int("NOTIFY_RETRY_DELAY_MS", prefix, suffix, "1000")
    if delay_ms < 0 or delay_ms > 30000:
        raise SystemExit(f"Invalid {name_d}={delay_ms} — must be 0-30000")

    return retries, delay_ms


def _is_retryable(exc: Exception) -> bool:
    """Return True if the exception is worth retrying (5xx, timeout, network)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    # Network errors, timeouts, etc. are retryable
    return isinstance(exc, httpx.HTTPError)


def _short_reason(exc: Exception) -> str:
    """Return a brief, subject-line-friendly reason from an exception."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def _format_alert_subject(
    notifier_name: str, exc: Exception, relay_name: str | None,
) -> str:
    relay_part = f" ({relay_name})" if relay_name else ""
    return f"[relayport] {notifier_name}{relay_part} failed: {_short_reason(exc)}"


def _format_alert_body(
    notifier: BaseNotifier,
    exc: Exception,
    *,
    relay_name: str | None,
    attempts: int,
) -> str:
    """Build the plain-text email body.

    Explicit field-by-field formatting (never ``str(payload)``) so a future
    change to a notifier cannot accidentally leak the trade payload into the
    alert email.
    """
    suffix = getattr(notifier, "_suffix", "") or "(none)"
    destination = getattr(notifier, "_url", "<unknown>")
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"Notifier:    {type(notifier).__name__}\n"
        f"Relay:       {relay_name or '(unknown)'}\n"
        f"Suffix:      {suffix}\n"
        f"Destination: {destination}\n"
        f"Attempts:    {attempts}\n"
        f"Failure:     {exc}\n"
        f"Time (UTC):  {timestamp}\n"
        "\n"
        "Trade payload is intentionally omitted (may contain account data).\n"
        "\n"
        "To diagnose, view logs on the droplet:\n"
        "  make logs S=relays\n"
        "or\n"
        "  ssh root@$DROPLET_IP "
        "\"cd /opt/relayport && docker compose logs --tail=200 relays\"\n"
    )


def notify(
    notifiers: list[BaseNotifier],
    payload: BaseModel,
    *,
    retries: int = 0,
    retry_delay_ms: int = 1000,
    relay_name: str | None = None,
) -> None:
    """Dispatch payload to all configured notifiers.

    Each backend is retried up to ``retries`` times on retryable failure
    (5xx, timeout, network error). 4xx errors are not retried.
    If at least one backend succeeds, return normally (log warnings for failures).
    If ALL backends fail, raise ``NotificationError`` — caller must NOT mark fills.

    A failed backend (after retries exhausted) also triggers an operational
    email alert via :mod:`relay_core.alerter` — best-effort, throttled, and
    fully optional (silent no-op when alert env vars are unset).
    """
    if not notifiers:
        log.info("No notifiers configured — skipping notification")
        return

    succeeded = 0
    failures: list[tuple[str, Exception]] = []

    for notifier in notifiers:
        last_exc: Exception | None = None
        for attempt in range(1 + retries):
            try:
                notifier.send(payload)
                succeeded += 1
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if attempt < retries and _is_retryable(exc):
                    delay_s = retry_delay_ms / 1000.0
                    log.warning(
                        "Notifier %s attempt %d/%d failed: %s — retrying in %.1fs",
                        type(notifier).__name__, attempt + 1, 1 + retries,
                        exc, delay_s,
                    )
                    time.sleep(delay_s)
                else:
                    # Non-retryable (e.g. 4xx) or last attempt — stop retrying
                    break

        if last_exc is not None:
            notifier_name = type(notifier).__name__
            log.error(
                "Notifier %s failed after %d attempt(s): %s",
                notifier_name, 1 + retries, last_exc,
            )
            failures.append((notifier_name, last_exc))
            suffix = getattr(notifier, "_suffix", "") or "-"
            send_alert(
                subject=_format_alert_subject(notifier_name, last_exc, relay_name),
                body=_format_alert_body(
                    notifier, last_exc,
                    relay_name=relay_name,
                    attempts=1 + retries,
                ),
                key=f"{notifier_name}:{relay_name or '-'}:{suffix}",
            )

    if succeeded == 0:
        raise NotificationError(failures)

    if failures:
        names = ", ".join(name for name, _ in failures)
        log.warning(
            "%d/%d notifier(s) failed: %s — fills will be marked processed"
            " (partial success)",
            len(failures), len(notifiers), names,
        )
