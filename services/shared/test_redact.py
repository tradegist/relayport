"""Tests for the redaction / safe-logging helpers."""

import httpx

from shared.redact import redact_url, safe_http_error_context


class TestRedactUrl:
    def test_masks_last_path_segment(self) -> None:
        assert (
            redact_url("https://discord.com/api/webhooks/123/SECRET_TOKEN")
            == "https://discord.com/api/webhooks/123/***"
        )

    def test_masks_slack_style_token(self) -> None:
        assert (
            redact_url("https://hooks.slack.com/services/T0/B0/SECRET")
            == "https://hooks.slack.com/services/T0/B0/***"
        )

    def test_drops_query_string(self) -> None:
        assert (
            redact_url("https://api.example.com/webhook?token=SECRET")
            == "https://api.example.com/***"
        )

    def test_drops_fragment(self) -> None:
        assert (
            redact_url("https://api.example.com/webhook/abc#frag")
            == "https://api.example.com/webhook/***"
        )

    def test_drops_query_and_fragment(self) -> None:
        assert (
            redact_url("https://api.example.com/webhook/abc?k=v#frag")
            == "https://api.example.com/webhook/***"
        )

    def test_handles_trailing_slash(self) -> None:
        # Trailing slash leaves the last segment empty — mask the one before.
        assert (
            redact_url("https://api.example.com/webhook/abc/")
            == "https://api.example.com/webhook/***/"
        )

    def test_root_path(self) -> None:
        # No path segment to mask — return host as-is.
        assert redact_url("https://example.com") == "https://example.com"

    def test_root_path_with_slash(self) -> None:
        assert redact_url("https://example.com/") == "https://example.com/"

    def test_passes_through_non_url_sentinel(self) -> None:
        assert redact_url("<unknown>") == "<unknown>"

    def test_passes_through_empty_string(self) -> None:
        assert redact_url("") == ""

    def test_passes_through_plain_text(self) -> None:
        # No scheme / netloc → return as-is, don't mangle.
        assert redact_url("not a url") == "not a url"

    def test_preserves_port(self) -> None:
        assert (
            redact_url("https://api.example.com:8443/webhook/SECRET")
            == "https://api.example.com:8443/webhook/***"
        )

    def test_http_scheme(self) -> None:
        assert (
            redact_url("http://internal.local/hook/abc")
            == "http://internal.local/hook/***"
        )

    def test_strips_basic_auth_credentials(self) -> None:
        assert (
            redact_url("https://user:password@api.example.com/hook/abc")
            == "https://api.example.com/hook/***"
        )

    def test_strips_username_only_userinfo(self) -> None:
        assert (
            redact_url("https://user@api.example.com/hook/abc")
            == "https://api.example.com/hook/***"
        )

    def test_strips_userinfo_and_preserves_port(self) -> None:
        assert (
            redact_url("https://user:pass@api.example.com:8443/hook/abc")
            == "https://api.example.com:8443/hook/***"
        )

    def test_ipv6_host_is_bracketed(self) -> None:
        assert (
            redact_url("https://[::1]:8080/hook/abc")
            == "https://[::1]:8080/hook/***"
        )

    def test_ipv6_host_with_userinfo(self) -> None:
        assert (
            redact_url("https://user:pass@[::1]:8080/hook/abc")
            == "https://[::1]:8080/hook/***"
        )


class TestSafeHttpErrorContext:
    @staticmethod
    def _resp(
        *, content: bytes = b"", headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return httpx.Response(
            status_code=400, headers=headers or {}, content=content,
        )

    def test_includes_json_body(self) -> None:
        resp = self._resp(
            content=b'{"error":"invalid_signature"}',
            headers={"Content-Type": "application/json"},
        )
        ctx = safe_http_error_context(resp)
        assert "invalid_signature" in ctx
        assert ctx.startswith("body: ")

    def test_includes_text_plain_body(self) -> None:
        resp = self._resp(
            content=b"quota exceeded",
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        ctx = safe_http_error_context(resp)
        assert "quota exceeded" in ctx

    def test_omits_html_body(self) -> None:
        """HTML error pages (often huge, often unrelated) are summarised only."""
        resp = self._resp(
            content=b"<html><body>500 Internal Server Error</body></html>",
            headers={"Content-Type": "text/html"},
        )
        ctx = safe_http_error_context(resp)
        assert "Internal Server Error" not in ctx
        assert "text/html" in ctx
        assert "omitted" in ctx

    def test_omits_binary_body(self) -> None:
        resp = self._resp(
            content=b"\x00\x01\x02PNG",
            headers={"Content-Type": "application/octet-stream"},
        )
        ctx = safe_http_error_context(resp)
        assert "PNG" not in ctx
        assert "application/octet-stream" in ctx
        assert "omitted" in ctx

    def test_omits_body_when_content_type_missing(self) -> None:
        resp = self._resp(content=b"some response")
        ctx = safe_http_error_context(resp)
        assert "some response" not in ctx
        assert "unknown" in ctx

    def test_empty_body_no_context(self) -> None:
        resp = self._resp()
        assert safe_http_error_context(resp) == ""

    def test_caps_long_text_body(self) -> None:
        big_body = "x" * 5000
        resp = self._resp(
            content=big_body.encode(),
            headers={"Content-Type": "text/plain"},
        )
        ctx = safe_http_error_context(resp)
        # Body is capped — must not contain the full 5000 chars.
        assert len(ctx) < 1000
        assert ctx.count("x") == 500

    def test_surfaces_request_id_header(self) -> None:
        resp = self._resp(
            content=b'{"err":"oops"}',
            headers={
                "Content-Type": "application/json",
                "X-Request-Id": "req_abc123",
            },
        )
        ctx = safe_http_error_context(resp)
        assert "x-request-id=req_abc123" in ctx
        assert "oops" in ctx

    def test_surfaces_correlation_id_header(self) -> None:
        resp = self._resp(
            content=b"err",
            headers={
                "Content-Type": "text/plain",
                "X-Correlation-Id": "corr_999",
            },
        )
        ctx = safe_http_error_context(resp)
        assert "x-correlation-id=corr_999" in ctx
