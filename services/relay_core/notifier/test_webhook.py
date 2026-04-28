"""Unit tests for WebhookNotifier."""

import hashlib
import hmac as hmac_mod
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from relay_core.notifier.webhook import WebhookNotifier


class _SamplePayload(BaseModel):
    symbol: str
    quantity: int


class TestWebhookNotifier:
    def test_dry_run_no_url(self) -> None:
        """No URL → logs payload, no HTTP call."""
        env = {"DEBUG_WEBHOOK_PATH": "test", "WEBHOOK_SECRET": "s"}
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier()
        # Force empty URL to exercise dry-run path
        notifier._url = ""
        notifier.send(_SamplePayload(symbol="AAPL", quantity=1))

    @patch("notifier.webhook.httpx.post")
    def test_sends_with_signature(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200)
        secret = "test-secret"
        env = {
            "TARGET_WEBHOOK_URL": "https://example.com/hook",
            "WEBHOOK_SECRET": secret,
        }
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier()

        payload = _SamplePayload(symbol="AAPL", quantity=1)
        notifier.send(payload)

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs["headers"]
        body = call_kwargs.kwargs["content"]

        expected_sig = hmac_mod.new(
            secret.encode(), body.encode(), hashlib.sha256
        ).hexdigest()
        assert headers["X-Signature-256"] == f"sha256={expected_sig}"
        assert headers["Content-Type"] == "application/json"

    @patch("notifier.webhook.httpx.post")
    def test_custom_header_sent(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(status_code=200)
        env = {
            "TARGET_WEBHOOK_URL": "https://example.com/hook",
            "WEBHOOK_SECRET": "s",
            "WEBHOOK_HEADER_NAME": "X-Custom",
            "WEBHOOK_HEADER_VALUE": "my-value",
        }
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier()

        notifier.send(_SamplePayload(symbol="AAPL", quantity=1))

        headers = mock_post.call_args.kwargs["headers"]
        assert headers["X-Custom"] == "my-value"

    @patch("notifier.webhook.httpx.post")
    def test_network_error_raises(self, mock_post: MagicMock) -> None:
        """Network failure propagates so callers can retry."""
        import httpx

        mock_post.side_effect = httpx.ConnectError("connection refused")
        env = {
            "TARGET_WEBHOOK_URL": "https://example.com/hook",
            "WEBHOOK_SECRET": "s",
        }
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier()

        with pytest.raises(httpx.ConnectError):
            notifier.send(_SamplePayload(symbol="AAPL", quantity=1))

    @patch("notifier.webhook.httpx.post")
    def test_5xx_raises_status_error(self, mock_post: MagicMock) -> None:
        """Server error (5xx) propagates via raise_for_status()."""
        import httpx

        resp = MagicMock()
        resp.status_code = 500
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=resp,
        )
        mock_post.return_value = resp
        env = {
            "TARGET_WEBHOOK_URL": "https://example.com/hook",
            "WEBHOOK_SECRET": "s",
        }
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier()

        with pytest.raises(httpx.HTTPStatusError):
            notifier.send(_SamplePayload(symbol="AAPL", quantity=1))

    @patch("notifier.webhook.httpx.post")
    def test_4xx_raises_status_error(self, mock_post: MagicMock) -> None:
        """Client error (4xx) propagates immediately."""
        import httpx

        resp = MagicMock()
        resp.status_code = 400
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "400", request=MagicMock(), response=resp,
        )
        mock_post.return_value = resp
        env = {
            "TARGET_WEBHOOK_URL": "https://example.com/hook",
            "WEBHOOK_SECRET": "s",
        }
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier()

        with pytest.raises(httpx.HTTPStatusError):
            notifier.send(_SamplePayload(symbol="AAPL", quantity=1))

    @patch("notifier.webhook.httpx.post")
    def test_4xx_includes_response_body_in_message(
        self, mock_post: MagicMock,
    ) -> None:
        """A text/plain response body must be surfaced in the exception message
        so downstream alerting (logs, emails) can show *why* the receiver
        rejected the request."""
        import httpx

        resp = MagicMock()
        resp.status_code = 400
        resp.text = "You've exceeded your daily quota"
        resp.content = resp.text.encode()
        resp.headers = {"Content-Type": "text/plain"}
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Client error '400 Bad Request' for url 'https://example.com/hook'",
            request=MagicMock(), response=resp,
        )
        mock_post.return_value = resp
        env = {
            "TARGET_WEBHOOK_URL": "https://example.com/hook",
            "WEBHOOK_SECRET": "s",
        }
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier()

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            notifier.send(_SamplePayload(symbol="AAPL", quantity=1))
        assert "You've exceeded your daily quota" in str(exc_info.value)

    @patch("notifier.webhook.httpx.post")
    def test_suffix_reads_suffixed_vars(self, mock_post: MagicMock) -> None:
        """Suffix=_2 reads TARGET_WEBHOOK_URL_2, etc."""
        mock_post.return_value = MagicMock(status_code=200)
        env = {
            "TARGET_WEBHOOK_URL_2": "https://example.com/hook2",
            "WEBHOOK_SECRET_2": "secret2",
        }
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier(suffix="_2")

        notifier.send(_SamplePayload(symbol="GOOG", quantity=5))

        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs["content"]  # body was sent

    @patch("notifier.webhook.httpx.post")
    def test_prefix_reads_prefixed_vars(self, mock_post: MagicMock) -> None:
        """Prefix=IBKR_ reads IBKR_TARGET_WEBHOOK_URL, etc."""
        mock_post.return_value = MagicMock(status_code=200)
        env = {
            "IBKR_TARGET_WEBHOOK_URL": "https://ibkr.com/hook",
            "IBKR_WEBHOOK_SECRET": "ibkr-secret",
        }
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier(prefix="IBKR_")

        assert notifier._url == "https://ibkr.com/hook"
        notifier.send(_SamplePayload(symbol="AAPL", quantity=1))
        mock_post.assert_called_once()

    @patch("notifier.webhook.httpx.post")
    def test_prefix_falls_back_to_generic(self, mock_post: MagicMock) -> None:
        """IBKR_ prefix set but only generic vars exist → uses generic."""
        mock_post.return_value = MagicMock(status_code=200)
        env = {
            "TARGET_WEBHOOK_URL": "https://generic.com/hook",
            "WEBHOOK_SECRET": "generic-secret",
        }
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier(prefix="IBKR_")

        assert notifier._url == "https://generic.com/hook"

    @patch("notifier.webhook.httpx.post")
    def test_prefix_overrides_generic(self, mock_post: MagicMock) -> None:
        """Prefixed var takes precedence over generic."""
        mock_post.return_value = MagicMock(status_code=200)
        env = {
            "TARGET_WEBHOOK_URL": "https://generic.com",
            "WEBHOOK_SECRET": "generic-s",
            "IBKR_TARGET_WEBHOOK_URL": "https://ibkr.com",
            "IBKR_WEBHOOK_SECRET": "ibkr-s",
        }
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier(prefix="IBKR_")

        assert notifier._url == "https://ibkr.com"

    @patch("notifier.webhook.httpx.post")
    def test_prefix_plus_suffix(self, mock_post: MagicMock) -> None:
        """Prefix and suffix compose: IBKR_TARGET_WEBHOOK_URL_2."""
        mock_post.return_value = MagicMock(status_code=200)
        env = {
            "IBKR_TARGET_WEBHOOK_URL_2": "https://ibkr-2.com",
            "IBKR_WEBHOOK_SECRET_2": "ibkr-s-2",
        }
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier(prefix="IBKR_", suffix="_2")

        assert notifier._url == "https://ibkr-2.com"

    @patch("notifier.webhook.httpx.post")
    def test_prefix_custom_header(self, mock_post: MagicMock) -> None:
        """Prefixed custom header vars are used."""
        mock_post.return_value = MagicMock(status_code=200)
        env = {
            "IBKR_TARGET_WEBHOOK_URL": "https://ibkr.com",
            "IBKR_WEBHOOK_SECRET": "s",
            "IBKR_WEBHOOK_HEADER_NAME": "X-IBKR",
            "IBKR_WEBHOOK_HEADER_VALUE": "ibkr-val",
        }
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier(prefix="IBKR_")

        notifier.send(_SamplePayload(symbol="AAPL", quantity=1))
        headers = mock_post.call_args.kwargs["headers"]
        assert headers["X-IBKR"] == "ibkr-val"

    def test_required_env_vars(self) -> None:
        assert "TARGET_WEBHOOK_URL" in WebhookNotifier.required_env_vars()
        assert "WEBHOOK_SECRET" in WebhookNotifier.required_env_vars()


class TestResolveWebhookUrl:
    def test_debug_path_overrides_target_url(self) -> None:
        env = {"DEBUG_WEBHOOK_PATH": "abc123", "TARGET_WEBHOOK_URL": "https://original.com", "WEBHOOK_SECRET": "s"}
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier()
        assert notifier._url == "http://debug:9000/debug/webhook/abc123"

    def test_no_debug_path_uses_target_url(self) -> None:
        env = {"TARGET_WEBHOOK_URL": "https://original.com/hook", "WEBHOOK_SECRET": "s"}
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier()
        assert notifier._url == "https://original.com/hook"

    def test_blank_debug_path_uses_target_url(self) -> None:
        env = {"DEBUG_WEBHOOK_PATH": "  ", "TARGET_WEBHOOK_URL": "https://keep.com", "WEBHOOK_SECRET": "s"}
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier()
        assert notifier._url == "https://keep.com"


class TestValidation:
    def test_missing_required_vars_exits(self) -> None:
        """Constructor raises SystemExit when required env vars are missing."""
        with patch.dict("os.environ", {}, clear=True), \
             pytest.raises(SystemExit):
            WebhookNotifier()

    def test_missing_secret_exits(self) -> None:
        """WEBHOOK_SECRET is required even when DEBUG_WEBHOOK_PATH is set."""
        env = {"DEBUG_WEBHOOK_PATH": "abc123"}
        with patch.dict("os.environ", env, clear=True), \
             pytest.raises(SystemExit):
            WebhookNotifier()

    def test_debug_path_skips_target_url_validation(self) -> None:
        """TARGET_WEBHOOK_URL is not required when DEBUG_WEBHOOK_PATH is set."""
        env = {"DEBUG_WEBHOOK_PATH": "abc123", "WEBHOOK_SECRET": "s"}
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier()
        assert "debug:9000" in notifier._url

    def test_missing_target_url_without_debug_exits(self) -> None:
        """TARGET_WEBHOOK_URL is required when DEBUG_WEBHOOK_PATH is not set."""
        env = {"WEBHOOK_SECRET": "s"}
        with patch.dict("os.environ", env, clear=True), \
             pytest.raises(SystemExit):
            WebhookNotifier()

    def test_debug_path_ignores_suffix(self) -> None:
        """DEBUG_WEBHOOK_PATH has no suffix — all instances share the same debug inbox."""
        env = {"DEBUG_WEBHOOK_PATH": "xyz", "TARGET_WEBHOOK_URL_2": "https://other.com", "WEBHOOK_SECRET_2": "s"}
        with patch.dict("os.environ", env, clear=True):
            notifier = WebhookNotifier(suffix="_2")
        assert notifier._url == "http://debug:9000/debug/webhook/xyz"
