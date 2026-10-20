"""Unit tests for the operational alerter."""

import threading
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from relay_core import alerter
from relay_core.alerter import _mask_email, _reset_for_test, send_alert


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Clear cooldown state before every test."""
    _reset_for_test()


_ENABLED_ENV = {
    "RESEND_API_KEY": "re_test_key",
    "ALERT_REPORT_EMAIL_TO": "ops@example.com",
}


class TestOptional:
    def test_noop_when_api_key_unset(self) -> None:
        env = {"ALERT_REPORT_EMAIL_TO": "ops@example.com"}
        with patch.dict("os.environ", env, clear=True), \
             patch("relay_core.alerter.httpx.post") as mock_post:
            send_alert(subject="s", body="b", key="k")
        mock_post.assert_not_called()

    def test_noop_when_recipient_unset(self) -> None:
        env = {"RESEND_API_KEY": "re_x"}
        with patch.dict("os.environ", env, clear=True), \
             patch("relay_core.alerter.httpx.post") as mock_post:
            send_alert(subject="s", body="b", key="k")
        mock_post.assert_not_called()

    def test_noop_when_both_unset(self) -> None:
        with patch.dict("os.environ", {}, clear=True), \
             patch("relay_core.alerter.httpx.post") as mock_post:
            send_alert(subject="s", body="b", key="k")
        mock_post.assert_not_called()


class TestSend:
    def test_first_failure_posts_to_resend(self) -> None:
        with patch.dict("os.environ", _ENABLED_ENV, clear=True), \
             patch("relay_core.alerter.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            send_alert(subject="alert subj", body="alert body", key="k1")

        mock_post.assert_called_once()
        call = mock_post.call_args
        assert call.args[0] == "https://api.resend.com/emails"
        assert call.kwargs["headers"]["Authorization"] == "Bearer re_test_key"
        body = call.kwargs["json"]
        assert body["to"] == ["ops@example.com"]
        assert body["subject"] == "alert subj"
        assert body["text"] == "alert body"
        assert body["from"] == "onboarding@resend.dev"
        assert call.kwargs["timeout"] == alerter._HTTP_TIMEOUT_S

    def test_custom_from_address(self) -> None:
        env = {**_ENABLED_ENV, "ALERT_EMAIL_FROM": "alerts@my.dev"}
        with patch.dict("os.environ", env, clear=True), \
             patch("relay_core.alerter.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            send_alert(subject="s", body="b", key="k")

        assert mock_post.call_args.kwargs["json"]["from"] == "alerts@my.dev"

    def test_resend_error_swallowed(self) -> None:
        """Non-2xx from Resend must not raise."""
        with patch.dict("os.environ", _ENABLED_ENV, clear=True), \
             patch("relay_core.alerter.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=500, text="internal error",
            )
            send_alert(subject="s", body="b", key="k")  # must not raise

    def test_network_error_swallowed(self) -> None:
        """httpx exceptions must not propagate."""
        with patch.dict("os.environ", _ENABLED_ENV, clear=True), \
             patch("relay_core.alerter.httpx.post") as mock_post:
            mock_post.side_effect = httpx.ConnectError("refused")
            send_alert(subject="s", body="b", key="k")  # must not raise


class TestCooldown:
    def test_second_failure_within_window_suppressed(self) -> None:
        with patch.dict("os.environ", _ENABLED_ENV, clear=True), \
             patch("relay_core.alerter.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            send_alert(subject="s1", body="b1", key="same")
            send_alert(subject="s2", body="b2", key="same")
        assert mock_post.call_count == 1

    def test_second_failure_after_window_fires(self) -> None:
        env = {**_ENABLED_ENV, "ALERT_COOLDOWN_MINUTES": "0"}
        with patch.dict("os.environ", env, clear=True), \
             patch("relay_core.alerter.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            send_alert(subject="s1", body="b1", key="same")
            send_alert(subject="s2", body="b2", key="same")
        assert mock_post.call_count == 2

    def test_different_keys_independent(self) -> None:
        with patch.dict("os.environ", _ENABLED_ENV, clear=True), \
             patch("relay_core.alerter.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            send_alert(subject="s", body="b", key="ibkr")
            send_alert(subject="s", body="b", key="kraken")
        assert mock_post.call_count == 2

    def test_failed_delivery_does_not_engage_cooldown(self) -> None:
        """A non-2xx response must not suppress the next attempt."""
        with patch.dict("os.environ", _ENABLED_ENV, clear=True), \
             patch("relay_core.alerter.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=500, text="boom")
            send_alert(subject="s1", body="b1", key="same")
            send_alert(subject="s2", body="b2", key="same")
        assert mock_post.call_count == 2

    def test_network_error_does_not_engage_cooldown(self) -> None:
        """An httpx exception must not suppress the next attempt."""
        with patch.dict("os.environ", _ENABLED_ENV, clear=True), \
             patch("relay_core.alerter.httpx.post") as mock_post:
            mock_post.side_effect = httpx.ConnectError("refused")
            send_alert(subject="s1", body="b1", key="same")
            send_alert(subject="s2", body="b2", key="same")
        assert mock_post.call_count == 2

    def test_invalid_cooldown_does_not_raise(self) -> None:
        """Invalid ALERT_COOLDOWN_MINUTES must not propagate to the caller.

        ``get_cooldown_minutes()`` raises SystemExit, but the alerter's
        outer try/except swallows it so a misconfigured env var can never
        crash the relay.
        """
        env = {**_ENABLED_ENV, "ALERT_COOLDOWN_MINUTES": "-1"}
        with patch.dict("os.environ", env, clear=True), \
             patch("relay_core.alerter.httpx.post"):
            send_alert(subject="s", body="b", key="k")  # must not raise


class TestRecipientMasking:
    @pytest.mark.parametrize(("address", "expected"), [
        ("ops@example.com", "o***@e***.com"),
        ("first.last@mail.example.co.uk", "f***@m***.uk"),
        ("root@localhost", "r***@l***"),
        ("not-an-address", "***"),
        ("@example.com", "***"),
        ("ops@", "***"),
    ])
    def test_mask_email(self, address: str, expected: str) -> None:
        assert _mask_email(address) == expected

    def test_sent_log_masks_recipient(self, caplog: pytest.LogCaptureFixture) -> None:
        with patch.dict("os.environ", _ENABLED_ENV, clear=True), \
             patch("relay_core.alerter.httpx.post") as mock_post, \
             caplog.at_level("INFO", logger="alerter"):
            mock_post.return_value = MagicMock(status_code=200)
            send_alert(subject="s", body="b", key="k")
        assert "Alert sent to o***@e***.com (HTTP 200)" in caplog.text
        assert "ops@example.com" not in caplog.text

    def test_failure_log_masks_recipient_echoed_in_body(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Padding pushes the address across the 200-char truncation point:
        # masking must happen first, or a partial address would survive.
        body = "x" * 185 + '"OPS@example.com" is not a valid recipient'
        with patch.dict("os.environ", _ENABLED_ENV, clear=True), \
             patch("relay_core.alerter.httpx.post") as mock_post, \
             caplog.at_level("ERROR", logger="alerter"):
            mock_post.return_value = MagicMock(status_code=422, text=body)
            send_alert(subject="s", body="b", key="k")
        assert "Alert delivery failed: HTTP 422" in caplog.text
        assert "ops@" not in caplog.text.lower()
        assert "example.co" not in caplog.text


class TestConcurrency:
    def test_concurrent_calls_same_key_fire_once(self) -> None:
        """Two threads racing on the same key should result in one POST."""
        env = {**_ENABLED_ENV, "ALERT_COOLDOWN_MINUTES": "60"}
        post_calls: list[float] = []

        def fake_post(*_args: object, **_kwargs: object) -> MagicMock:
            # Hold briefly so both threads have a chance to enter the
            # critical section sequentially — verifies the lock works.
            post_calls.append(time.monotonic())
            return MagicMock(status_code=200)

        with patch.dict("os.environ", env, clear=True), \
             patch("relay_core.alerter.httpx.post", side_effect=fake_post):
            threads = [
                threading.Thread(
                    target=send_alert,
                    kwargs={"subject": "s", "body": "b", "key": "shared"},
                )
                for _ in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(post_calls) == 1
