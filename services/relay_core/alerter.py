"""Operational alerter — email the operator when a notifier fails to deliver.

Strictly best-effort: never raises, never affects the retry/mark-processed
contract.  Calls are silent no-ops when the alerting env vars are unset
(opt-in only).

Throttling: the first failure for a given key fires immediately; subsequent
failures within ``ALERT_COOLDOWN_MINUTES`` (default 60) are suppressed.
State is in-memory and resets on container restart — a restart with a still-
broken destination re-fires once, which is itself useful signal.
"""

import logging
import threading
import time

import httpx

from relay_core.env import get_env, get_env_int

log = logging.getLogger("alerter")

_RESEND_API_URL = "https://api.resend.com/emails"
_DEFAULT_FROM = "onboarding@resend.dev"
_HTTP_TIMEOUT_S = 5.0

_last_alert_at: dict[str, float] = {}
_lock = threading.Lock()


def _get_resend_api_key() -> str:
    return get_env("RESEND_API_KEY")


def _get_alert_to() -> str:
    return get_env("ALERT_REPORT_EMAIL_TO")


def _get_alert_from() -> str:
    return get_env("ALERT_EMAIL_FROM") or _DEFAULT_FROM


def _get_cooldown_seconds() -> int:
    """Read ALERT_COOLDOWN_MINUTES (default 60) and return seconds."""
    name, minutes = get_env_int("ALERT_COOLDOWN_MINUTES", default="60")
    if minutes < 0:
        raise SystemExit(f"Invalid {name}={minutes} — must be >= 0")
    return minutes * 60


def send_alert(*, subject: str, body: str, key: str) -> None:
    """Email the operator about a delivery failure.

    Silent no-op when ``RESEND_API_KEY`` or ``ALERT_REPORT_EMAIL_TO`` is unset.
    Throttled per ``key`` to prevent flooding.  Catches every exception
    internally — alerting must never propagate failure to the caller.

    Args:
        subject: Inbox-friendly subject line.
        body: Plain-text body. Must NOT include trade payloads or secrets.
        key: Throttling key (e.g. ``"WebhookNotifier:ibkr:-"``). Callers
             with the same key share a cooldown window.
    """
    try:
        api_key = _get_resend_api_key()
        to = _get_alert_to()
        if not api_key or not to:
            return

        cooldown_s = _get_cooldown_seconds()
        now = time.monotonic()
        with _lock:
            last = _last_alert_at.get(key, 0.0)
            if last and (now - last) < cooldown_s:
                log.debug("Alert suppressed (cooldown active for key=%r)", key)
                return
            # Optimistic claim: setting the timestamp inside the lock prevents
            # concurrent callers from POSTing the same alert. Rolled back below
            # if delivery fails so a broken destination doesn't suppress retries.
            _last_alert_at[key] = now

        delivered = False
        try:
            payload = {
                "from": _get_alert_from(),
                "to": [to],
                "subject": subject,
                "text": body,
            }
            resp = httpx.post(
                _RESEND_API_URL,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=_HTTP_TIMEOUT_S,
            )
            if 200 <= resp.status_code < 300:
                log.info("Alert sent to %s (HTTP %d)", to, resp.status_code)
                delivered = True
            else:
                log.error(
                    "Alert delivery failed: HTTP %d body=%s",
                    resp.status_code, resp.text[:200],
                )
        finally:
            if not delivered:
                with _lock:
                    # Only roll back our own claim — a later caller may have
                    # already retried and recorded a fresh timestamp.
                    if _last_alert_at.get(key) == now:
                        _last_alert_at.pop(key, None)
    except SystemExit as exc:
        # Env-var validation (cooldown parse, etc.) raises SystemExit — log
        # and swallow so a misconfigured alert var never crashes the relay.
        log.error("Alert delivery skipped: %s", exc)
    except Exception:
        log.exception("Alert delivery failed")


def _reset_for_test() -> None:
    """Clear the cooldown state.  Test helper only."""
    with _lock:
        _last_alert_at.clear()
