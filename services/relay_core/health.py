"""Outage tracking — email the operator when a relay component stays down.

Each component (one per poller, one per listener) owns an ``OutageTracker``
and reports every health observation to it: ``record_failure(reason)`` or
``record_success()``. A failure streak shorter than ``alert_after_s`` stays
silent, so routine blips (a broker's nightly restart, one 503) never alert.

Once the streak reaches ``alert_after_s``, every further failure returns an
``OutageAlert``. A failure flagged ``needs_action`` (the broker rejected the
configuration — no retry can fix it) skips the wait and alerts at once. ``send_alert``'s per-key cooldown (``ALERT_COOLDOWN_MINUTES``)
turns those into periodic reminders for as long as the outage lasts — and
retries on the next failure if an email could not be delivered. The first
success after an alerted outage returns a recovery alert.

Trackers decide, callers dispatch: ``send_alert`` blocks on HTTP, so async
callers send from ``asyncio.to_thread``.

State is in-memory. A relay restart mid-outage restarts the streak clock
(another ``alert_after_s`` before the next alert) and forgets that an
alert was sent, so that outage's recovery sends no email.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from relay_core.alerter import get_cooldown_minutes
from relay_core.env import get_env_int
from shared import RelayName

log = logging.getLogger(__name__)

DEFAULT_OUTAGE_ALERT_MINUTES = 15


def get_outage_alert_after_s(relay_name: RelayName) -> int:
    """Read {RELAY}_OUTAGE_ALERT_MINUTES (fallback OUTAGE_ALERT_MINUTES).

    Returns seconds. Default 15 minutes; 0 disables outage alerts.
    """
    prefix = f"{relay_name.upper()}_"
    var_name, minutes = get_env_int(
        "OUTAGE_ALERT_MINUTES", prefix, default=str(DEFAULT_OUTAGE_ALERT_MINUTES),
    )
    if minutes < 0:
        raise SystemExit(
            f"Invalid {var_name}={minutes} — must be >= 0 (0 disables outage alerts)"
        )
    return minutes * 60


def format_duration(seconds: float) -> str:
    """Render a duration for humans: ``45s``, ``16m``, ``27h 05m``."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _reminder_cadence() -> str:
    """How often reminders arrive — ``send_alert``'s cooldown sets the pace."""
    try:
        minutes = get_cooldown_minutes()
    except SystemExit as exc:
        # send_alert rejects the same value and skips delivery (logging
        # why), so this text never reaches anyone — any wording will do.
        log.debug("Cannot render the reminder cadence: %s", exc)
        return "periodically"
    if minutes == 0:
        return "on every failed check"
    return f"about every {minutes} minute{'' if minutes == 1 else 's'}"


@dataclass(frozen=True, slots=True)
class OutageAlert:
    """An email to send via ``relay_core.alerter.send_alert``."""

    subject: str
    body: str
    key: str


class OutageTracker:
    """Failure-streak state for one component, e.g. ``("ibkr", "listener")``.

    Thread-safe: the poller reports from ``asyncio.to_thread`` workers.
    *clock* is injectable for tests; it must be monotonic.
    """

    def __init__(
        self,
        relay_name: RelayName,
        component: str,
        alert_after_s: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._relay_name = relay_name
        self._component = component
        self._alert_after_s = alert_after_s
        self._clock = clock
        self._key = f"outage:{relay_name}:{component}"
        self._lock = threading.Lock()
        self._down_since: float | None = None
        self._down_since_utc = ""
        self._failures = 0
        self._alerted = False

    def record_failure(
        self, reason: str, *, needs_action: bool = False,
    ) -> OutageAlert | None:
        """Count a failed check; return an alert once the streak is long enough.

        *needs_action* marks a failure no retry can fix (expired token,
        invalid query): it alerts at once instead of after ``alert_after_s``,
        unless outage alerts are disabled (``alert_after_s == 0``).

        *reason* goes into the email verbatim — it must not contain
        secrets, account ids or raw payloads.
        """
        with self._lock:
            now = self._clock()
            if self._down_since is None:
                self._down_since = now
                self._down_since_utc = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
                self._failures = 0
            self._failures += 1
            elapsed = now - self._down_since
            if not self._alert_after_s:
                return None
            if not needs_action and elapsed < self._alert_after_s:
                return None
            first_alert = not self._alerted
            self._alerted = True
            failures = self._failures
            since = self._down_since_utc

        if needs_action:
            if first_alert:
                log.error(
                    "[%s] %s needs action: %s", self._relay_name, self._component, reason,
                )
            return OutageAlert(
                subject=f"[relayport] {self._relay_name}: {self._component} needs action",
                body=(
                    f"The broker rejected the {self._relay_name} {self._component}: "
                    f"{reason}\n\n"
                    f"Retrying will not fix this. Update the relay's configuration "
                    f"(e.g. renew the API token) and redeploy. Failing since "
                    f"{since} ({failures} failed check{'' if failures == 1 else 's'}"
                    f" in a row).\n\n"
                    f"Fills that depend on it are not being relayed until it "
                    f"recovers. You will get a reminder {_reminder_cadence()} "
                    f"while it keeps failing, and an email when it recovers.\n"
                ),
                key=self._key,
            )

        duration = format_duration(elapsed)
        if first_alert:
            log.error(
                "[%s] %s down for %s (%d failed checks): %s",
                self._relay_name, self._component, duration, failures, reason,
            )
        return OutageAlert(
            subject=f"[relayport] {self._relay_name}: {self._component} down for {duration}",
            body=(
                f"The {self._relay_name} {self._component} has been failing since "
                f"{since} ({duration}, {failures} failed checks in a row).\n\n"
                f"Last error: {reason}\n\n"
                f"Fills that depend on it are not being relayed until it "
                f"recovers. You will get a reminder {_reminder_cadence()} "
                f"while the outage lasts, and an email when it recovers. Check "
                f"the relay container logs for details.\n"
            ),
            key=self._key,
        )

    def record_success(self) -> OutageAlert | None:
        """Close any open failure streak; return a recovery alert if one was sent."""
        with self._lock:
            if self._down_since is None:
                return None
            elapsed = self._clock() - self._down_since
            alerted = self._alerted
            since = self._down_since_utc
            self._down_since = None
            self._down_since_utc = ""
            self._failures = 0
            self._alerted = False

        duration = format_duration(elapsed)
        log.info("[%s] %s recovered after %s", self._relay_name, self._component, duration)
        if not alerted:
            return None
        return OutageAlert(
            subject=f"[relayport] {self._relay_name}: {self._component} recovered",
            body=(
                f"The {self._relay_name} {self._component} is healthy again after "
                f"{duration} (down since {since}).\n\n"
                f"Check that fills executed during the outage were delivered: a "
                f"poller catches up within its lookback window, but fills only "
                f"a listener could see may need a manual resend.\n"
            ),
            key=f"{self._key}:recovered",
        )
