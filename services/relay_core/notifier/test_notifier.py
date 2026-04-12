"""Unit tests for notifier registry, loader, and dispatcher."""

from typing import cast
from unittest.mock import MagicMock, patch

import httpx
import pytest
from pydantic import BaseModel

from relay_core.notifier import (
    REGISTRY,
    NotificationError,
    load_notifiers,
    load_retry_config,
    notify,
)
from relay_core.notifier.webhook import WebhookNotifier


class _SamplePayload(BaseModel):
    symbol: str


class TestRegistry:
    def test_webhook_registered(self) -> None:
        assert "webhook" in REGISTRY

    def test_registry_values_are_classes(self) -> None:
        from relay_core.notifier.base import BaseNotifier

        for cls in REGISTRY.values():
            assert issubclass(cls, BaseNotifier)


class TestLoadNotifiers:
    def test_empty_env_returns_empty(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = load_notifiers()
        assert result == []

    def test_blank_env_returns_empty(self) -> None:
        with patch.dict("os.environ", {"NOTIFIERS": "  "}, clear=True):
            result = load_notifiers()
        assert result == []

    def test_unknown_name_exits(self) -> None:
        with patch.dict("os.environ", {"NOTIFIERS": "bogus"}, clear=True), \
             pytest.raises(SystemExit):
            load_notifiers()

    def test_missing_required_vars_exits(self) -> None:
        with patch.dict("os.environ", {"NOTIFIERS": "webhook"}, clear=True), \
             pytest.raises(SystemExit):
            load_notifiers()

    def test_valid_config_returns_instances(self) -> None:
        env = {
            "NOTIFIERS": "webhook",
            "TARGET_WEBHOOK_URL": "https://example.com/hook",
            "WEBHOOK_SECRET": "s",
        }
        with patch.dict("os.environ", env, clear=True):
            result = load_notifiers()
        assert len(result) == 1
        assert result[0].name == "webhook"

    def test_suffix_reads_suffixed_vars(self) -> None:
        env = {
            "NOTIFIERS_2": "webhook",
            "TARGET_WEBHOOK_URL_2": "https://example.com/hook2",
            "WEBHOOK_SECRET_2": "secret2",
        }
        with patch.dict("os.environ", env, clear=True):
            result = load_notifiers(suffix="_2")
        assert len(result) == 1

    def test_prefix_reads_prefixed_vars(self) -> None:
        env = {
            "IBKR_NOTIFIERS": "webhook",
            "IBKR_TARGET_WEBHOOK_URL": "https://example.com/ibkr",
            "IBKR_WEBHOOK_SECRET": "ibkr-secret",
        }
        with patch.dict("os.environ", env, clear=True):
            result = load_notifiers(prefix="IBKR_")
        assert len(result) == 1
        assert result[0].name == "webhook"

    def test_prefix_falls_back_to_generic(self) -> None:
        """IBKR_NOTIFIERS unset → falls back to NOTIFIERS."""
        env = {
            "NOTIFIERS": "webhook",
            "TARGET_WEBHOOK_URL": "https://example.com/hook",
            "WEBHOOK_SECRET": "s",
        }
        with patch.dict("os.environ", env, clear=True):
            result = load_notifiers(prefix="IBKR_")
        assert len(result) == 1

    def test_prefix_overrides_generic(self) -> None:
        """IBKR_NOTIFIERS is set → generic NOTIFIERS is ignored."""
        env = {
            "NOTIFIERS": "webhook",
            "TARGET_WEBHOOK_URL": "https://generic.com",
            "WEBHOOK_SECRET": "generic-s",
            "IBKR_NOTIFIERS": "webhook",
            "IBKR_TARGET_WEBHOOK_URL": "https://ibkr.com",
            "IBKR_WEBHOOK_SECRET": "ibkr-s",
        }
        with patch.dict("os.environ", env, clear=True):
            result = load_notifiers(prefix="IBKR_")
        assert len(result) == 1
        # The notifier should have used the IBKR-prefixed URL
        assert cast(WebhookNotifier, result[0])._url == "https://ibkr.com"

    def test_prefix_plus_suffix(self) -> None:
        """Prefix and suffix compose: IBKR_TARGET_WEBHOOK_URL_2."""
        env = {
            "IBKR_NOTIFIERS_2": "webhook",
            "IBKR_TARGET_WEBHOOK_URL_2": "https://ibkr-2.com",
            "IBKR_WEBHOOK_SECRET_2": "ibkr-s-2",
        }
        with patch.dict("os.environ", env, clear=True):
            result = load_notifiers(prefix="IBKR_", suffix="_2")
        assert len(result) == 1

    def test_prefix_empty_falls_back_to_generic_dry_run(self) -> None:
        """Prefix set but no IBKR_NOTIFIERS and no NOTIFIERS → dry-run."""
        with patch.dict("os.environ", {}, clear=True):
            result = load_notifiers(prefix="IBKR_")
        assert result == []


class TestNotify:
    def test_dispatches_to_all(self) -> None:
        n1 = MagicMock()
        n2 = MagicMock()
        payload = _SamplePayload(symbol="AAPL")

        notify([n1, n2], payload)

        n1.send.assert_called_once_with(payload)
        n2.send.assert_called_once_with(payload)

    def test_empty_list_is_noop(self) -> None:
        notify([], _SamplePayload(symbol="AAPL"))  # should not raise

    def test_all_fail_raises_notification_error(self) -> None:
        n1 = MagicMock()
        n1.send.side_effect = RuntimeError("boom")
        payload = _SamplePayload(symbol="AAPL")

        with pytest.raises(NotificationError) as exc_info:
            notify([n1], payload)
        assert len(exc_info.value.failures) == 1

    def test_partial_success_does_not_raise(self) -> None:
        """One backend fails, one succeeds → no exception."""
        n1 = MagicMock()
        n1.send.side_effect = RuntimeError("boom")
        n2 = MagicMock()
        payload = _SamplePayload(symbol="AAPL")

        notify([n1, n2], payload)  # should not raise

        n2.send.assert_called_once_with(payload)

    @patch("relay_core.notifier.time.sleep")
    def test_retries_on_retryable_error(self, mock_sleep: MagicMock) -> None:
        """5xx/network errors are retried up to retries count."""
        n1 = MagicMock()
        n1.send.side_effect = [
            httpx.ConnectError("refused"),
            None,  # second attempt succeeds
        ]
        payload = _SamplePayload(symbol="AAPL")

        notify([n1], payload, retries=2, retry_delay_ms=500)

        assert n1.send.call_count == 2
        mock_sleep.assert_called_once_with(0.5)

    @patch("relay_core.notifier.time.sleep")
    def test_no_retry_on_4xx(self, mock_sleep: MagicMock) -> None:
        """4xx errors are not retried — they fail immediately."""
        resp = MagicMock()
        resp.status_code = 400
        n1 = MagicMock()
        n1.send.side_effect = httpx.HTTPStatusError(
            "400", request=MagicMock(), response=resp,
        )
        payload = _SamplePayload(symbol="AAPL")

        with pytest.raises(NotificationError):
            notify([n1], payload, retries=3)

        assert n1.send.call_count == 1  # no retries
        mock_sleep.assert_not_called()

    @patch("relay_core.notifier.time.sleep")
    def test_retries_exhausted_raises(self, mock_sleep: MagicMock) -> None:
        """All retries fail → NotificationError."""
        n1 = MagicMock()
        n1.send.side_effect = httpx.ConnectError("refused")
        payload = _SamplePayload(symbol="AAPL")

        with pytest.raises(NotificationError):
            notify([n1], payload, retries=2, retry_delay_ms=100)

        assert n1.send.call_count == 3  # initial + 2 retries
        assert mock_sleep.call_count == 2

    def test_no_retry_when_retries_zero(self) -> None:
        """retries=0 means no retries — single attempt only."""
        n1 = MagicMock()
        n1.send.side_effect = httpx.ConnectError("refused")
        payload = _SamplePayload(symbol="AAPL")

        with pytest.raises(NotificationError):
            notify([n1], payload, retries=0)

        assert n1.send.call_count == 1


class TestLoadRetryConfig:
    def test_defaults(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            retries, delay = load_retry_config()
        assert retries == 0
        assert delay == 1000

    def test_reads_env_vars(self) -> None:
        env = {"NOTIFY_RETRIES": "3", "NOTIFY_RETRY_DELAY_MS": "2000"}
        with patch.dict("os.environ", env, clear=True):
            retries, delay = load_retry_config()
        assert retries == 3
        assert delay == 2000

    def test_prefix_overrides_generic(self) -> None:
        env = {
            "NOTIFY_RETRIES": "1",
            "IBKR_NOTIFY_RETRIES": "3",
            "NOTIFY_RETRY_DELAY_MS": "500",
            "IBKR_NOTIFY_RETRY_DELAY_MS": "2000",
        }
        with patch.dict("os.environ", env, clear=True):
            retries, delay = load_retry_config(prefix="IBKR_")
        assert retries == 3
        assert delay == 2000

    def test_prefix_falls_back_to_generic(self) -> None:
        env = {"NOTIFY_RETRIES": "2", "NOTIFY_RETRY_DELAY_MS": "1500"}
        with patch.dict("os.environ", env, clear=True):
            retries, delay = load_retry_config(prefix="IBKR_")
        assert retries == 2
        assert delay == 1500

    def test_retries_out_of_range_raises(self) -> None:
        env = {"NOTIFY_RETRIES": "10"}
        with patch.dict("os.environ", env, clear=True), \
             pytest.raises(SystemExit, match="0-5"):
            load_retry_config()

    def test_negative_retries_raises(self) -> None:
        env = {"NOTIFY_RETRIES": "-1"}
        with patch.dict("os.environ", env, clear=True), \
             pytest.raises(SystemExit, match="0-5"):
            load_retry_config()

    def test_delay_out_of_range_raises(self) -> None:
        env = {"NOTIFY_RETRY_DELAY_MS": "50000"}
        with patch.dict("os.environ", env, clear=True), \
             pytest.raises(SystemExit, match="0-30000"):
            load_retry_config()

    def test_invalid_value_raises(self) -> None:
        env = {"NOTIFY_RETRIES": "abc"}
        with patch.dict("os.environ", env, clear=True), \
             pytest.raises(SystemExit, match="must be an integer"):
            load_retry_config()
