"""Unit tests for relay_core.health (outage tracking)."""

import os
from unittest.mock import patch

import pytest

from relay_core.health import (
    DEFAULT_OUTAGE_ALERT_MINUTES,
    OutageTracker,
    format_duration,
    get_outage_alert_after_s,
)


class _Clock:
    """Manually advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _tracker(alert_after_s: int = 900) -> tuple[OutageTracker, _Clock]:
    clock = _Clock()
    return OutageTracker("ibkr", "listener", alert_after_s, clock=clock), clock


# ── get_outage_alert_after_s ─────────────────────────────────────────


class TestGetOutageAlertAfterS:
    def test_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert get_outage_alert_after_s("ibkr") == DEFAULT_OUTAGE_ALERT_MINUTES * 60

    def test_generic_var(self) -> None:
        with patch.dict(os.environ, {"OUTAGE_ALERT_MINUTES": "30"}, clear=True):
            assert get_outage_alert_after_s("ibkr") == 1800

    def test_relay_prefix_wins(self) -> None:
        env = {"OUTAGE_ALERT_MINUTES": "30", "KRAKEN_OUTAGE_ALERT_MINUTES": "5"}
        with patch.dict(os.environ, env, clear=True):
            assert get_outage_alert_after_s("kraken") == 300
            assert get_outage_alert_after_s("ibkr") == 1800

    def test_zero_disables(self) -> None:
        with patch.dict(os.environ, {"OUTAGE_ALERT_MINUTES": "0"}, clear=True):
            assert get_outage_alert_after_s("ibkr") == 0

    def test_negative_fails_fast(self) -> None:
        with patch.dict(os.environ, {"IBKR_OUTAGE_ALERT_MINUTES": "-1"}, clear=True), \
             pytest.raises(SystemExit, match="IBKR_OUTAGE_ALERT_MINUTES=-1"):
            get_outage_alert_after_s("ibkr")

    def test_non_integer_fails_fast(self) -> None:
        with patch.dict(os.environ, {"OUTAGE_ALERT_MINUTES": "soon"}, clear=True), \
             pytest.raises(SystemExit, match="must be an integer"):
            get_outage_alert_after_s("ibkr")


# ── format_duration ──────────────────────────────────────────────────


@pytest.mark.parametrize(("seconds", "expected"), [
    (0, "0s"),
    (45.9, "45s"),
    (60, "1m"),
    (16 * 60 + 30, "16m"),
    (3600, "1h 00m"),
    (27 * 3600 + 5 * 60, "27h 05m"),
])
def test_format_duration(seconds: float, expected: str) -> None:
    assert format_duration(seconds) == expected


# ── OutageTracker ────────────────────────────────────────────────────


class TestOutageTracker:
    def test_success_without_outage_is_silent(self) -> None:
        tracker, _ = _tracker()
        assert tracker.record_success() is None

    def test_failures_below_threshold_are_silent(self) -> None:
        tracker, clock = _tracker(alert_after_s=900)
        assert tracker.record_failure("down") is None
        clock.now += 899
        assert tracker.record_failure("down") is None

    def test_alerts_once_streak_reaches_threshold(self) -> None:
        tracker, clock = _tracker(alert_after_s=900)
        tracker.record_failure("first")
        clock.now += 900
        alert = tracker.record_failure("Flex SendRequest failed: [1012] Token has expired")
        assert alert is not None
        assert alert.subject == "[relayport] ibkr: listener down for 15m"
        assert alert.key == "outage:ibkr:listener"
        assert "[1012] Token has expired" in alert.body
        assert "2 failed checks in a row" in alert.body

    @pytest.mark.parametrize(("cooldown", "cadence"), [
        (None, "about every 60 minutes"),
        ("30", "about every 30 minutes"),
        ("1", "about every 1 minute "),
        ("0", "on every failed check"),
        ("-5", "periodically"),
    ])
    def test_body_renders_reminder_cadence(self, cooldown: str | None, cadence: str) -> None:
        env = {} if cooldown is None else {"ALERT_COOLDOWN_MINUTES": cooldown}
        tracker, clock = _tracker(alert_after_s=900)
        tracker.record_failure("down")
        clock.now += 900
        with patch.dict(os.environ, env, clear=True):
            alert = tracker.record_failure("down")
        assert alert is not None
        assert f"You will get a reminder {cadence}" in alert.body
        assert "ALERT_COOLDOWN_MINUTES" not in alert.body

    def test_keeps_alerting_while_down(self) -> None:
        """Every failure past the threshold re-emits; send_alert's cooldown throttles."""
        tracker, clock = _tracker(alert_after_s=900)
        tracker.record_failure("down")
        clock.now += 900
        assert tracker.record_failure("down") is not None
        clock.now += 3600
        reminder = tracker.record_failure("still down")
        assert reminder is not None
        assert reminder.subject == "[relayport] ibkr: listener down for 1h 15m"
        assert reminder.key == "outage:ibkr:listener"

    def test_first_alert_is_logged_once(self, caplog: pytest.LogCaptureFixture) -> None:
        tracker, clock = _tracker(alert_after_s=60)
        tracker.record_failure("down")
        clock.now += 60
        with caplog.at_level("ERROR", logger="relay_core.health"):
            tracker.record_failure("down")
            clock.now += 60
            tracker.record_failure("down")
        assert sum("listener down for" in r.getMessage() for r in caplog.records) == 1

    def test_recovery_after_alert_returns_recovery_alert(self) -> None:
        tracker, clock = _tracker(alert_after_s=900)
        tracker.record_failure("down")
        clock.now += 1000
        tracker.record_failure("down")
        recovery = tracker.record_success()
        assert recovery is not None
        assert recovery.subject == "[relayport] ibkr: listener recovered"
        assert recovery.key == "outage:ibkr:listener:recovered"
        assert "after 16m" in recovery.body

    def test_short_blip_recovers_silently(self) -> None:
        tracker, clock = _tracker(alert_after_s=900)
        tracker.record_failure("nightly restart")
        clock.now += 80
        assert tracker.record_success() is None

    def test_recovery_resets_the_streak(self) -> None:
        tracker, clock = _tracker(alert_after_s=900)
        tracker.record_failure("down")
        clock.now += 1000
        tracker.record_failure("down")
        tracker.record_success()
        # A new outage times from its own first failure, not the old one.
        clock.now += 10
        assert tracker.record_failure("down again") is None
        assert tracker.record_success() is None

    def test_needs_action_alerts_on_first_failure(self) -> None:
        tracker, _ = _tracker(alert_after_s=900)
        alert = tracker.record_failure(
            "Flex SendRequest failed: [1012] Token has expired.", needs_action=True,
        )
        assert alert is not None
        assert alert.subject == "[relayport] ibkr: listener needs action"
        assert alert.key == "outage:ibkr:listener"
        assert "[1012] Token has expired." in alert.body
        assert "Retrying will not fix this" in alert.body
        assert "(1 failed check in a row)" in alert.body

    def test_needs_action_with_pause_states_it_and_paces_reminders(self) -> None:
        tracker, _ = _tracker(alert_after_s=900)
        with patch.dict(os.environ, {}, clear=True):  # default 60-minute cooldown
            alert = tracker.record_failure("expired", needs_action=True, retry_in_s=7200)
        assert alert is not None
        assert "it now retries only every 2h 00m" in alert.body
        assert "You will get a reminder about every 2h 00m" in alert.body

    def test_needs_action_without_pause_keeps_cooldown_cadence(self) -> None:
        tracker, _ = _tracker(alert_after_s=900)
        with patch.dict(os.environ, {}, clear=True):
            alert = tracker.record_failure("expired", needs_action=True)
        assert alert is not None
        assert "retries only every" not in alert.body
        assert "You will get a reminder about every 60 minutes" in alert.body

    def test_retry_shorter_than_cooldown_keeps_cooldown_cadence(self) -> None:
        tracker, _ = _tracker(alert_after_s=900)
        with patch.dict(os.environ, {}, clear=True):
            alert = tracker.record_failure("expired", needs_action=True, retry_in_s=600)
        assert alert is not None
        assert "You will get a reminder about every 60 minutes" in alert.body

    def test_needs_action_keeps_alerting_with_same_key(self) -> None:
        """Reminders share the key, so send_alert's cooldown throttles them."""
        tracker, clock = _tracker(alert_after_s=900)
        tracker.record_failure("expired", needs_action=True)
        clock.now += 600
        reminder = tracker.record_failure("expired", needs_action=True)
        assert reminder is not None
        assert reminder.key == "outage:ibkr:listener"
        assert "(2 failed checks in a row)" in reminder.body

    def test_needs_action_after_plain_failures_alerts_at_once(self) -> None:
        tracker, clock = _tracker(alert_after_s=900)
        assert tracker.record_failure("503") is None
        clock.now += 60
        assert tracker.record_failure("expired", needs_action=True) is not None

    def test_needs_action_outage_sends_recovery(self) -> None:
        tracker, clock = _tracker(alert_after_s=900)
        tracker.record_failure("expired", needs_action=True)
        clock.now += 120
        recovery = tracker.record_success()
        assert recovery is not None
        assert recovery.subject == "[relayport] ibkr: listener recovered"

    def test_needs_action_respects_disabled_alerts(self) -> None:
        tracker, _ = _tracker(alert_after_s=0)
        assert tracker.record_failure("expired", needs_action=True) is None
        assert tracker.record_success() is None

    def test_needs_action_logged_once(self, caplog: pytest.LogCaptureFixture) -> None:
        tracker, _ = _tracker(alert_after_s=900)
        with caplog.at_level("ERROR", logger="relay_core.health"):
            tracker.record_failure("expired", needs_action=True)
            tracker.record_failure("expired", needs_action=True)
        assert sum("needs action" in r.getMessage() for r in caplog.records) == 1

    def test_zero_threshold_never_alerts(self) -> None:
        tracker, clock = _tracker(alert_after_s=0)
        tracker.record_failure("down")
        clock.now += 10 * 86400
        assert tracker.record_failure("down") is None
        assert tracker.record_success() is None
