"""Helpers for producing safe-to-log content from sensitive sources."""

from urllib.parse import urlparse

import httpx

# Only include response body excerpts for content-types that are
# expected to carry structured / human-readable error context.  Larger
# or unstructured types (HTML error pages, binary blobs) are summarised
# by length only so arbitrary receiver output never lands in operator
# logs or alert emails.
_SAFE_BODY_CONTENT_TYPES = frozenset({"application/json", "text/plain"})
_BODY_EXCERPT_MAX_CHARS = 500
_REQUEST_ID_HEADERS = ("X-Request-Id", "X-Correlation-Id", "Request-Id")


def redact_url(url: str) -> str:
    """Redact likely-sensitive parts of a URL for safe inclusion in logs/alerts.

    URLs commonly embed secrets in three places:

    * the userinfo prefix (``https://user:password@host``) — dropped entirely.
    * the last path segment (Slack/Discord webhook tokens) — masked as ``***``.
    * the query string (``?token=...``) and fragment — dropped entirely.

    Host (with IPv6 bracketing) and any leading path segments are kept so the
    operator can still identify which destination the URL points to.

    Returns the input unchanged when it does not parse as a URL (e.g. a
    sentinel like ``"<unknown>"`` or an empty string), so callers can pass
    arbitrary ``getattr(..., default)`` values without pre-checking.
    """
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return url
        # Reconstruct netloc from hostname + port to drop any userinfo
        # (``user:password@``) that would otherwise leak basic-auth creds.
        hostname = parsed.hostname
        if not hostname:
            return url
        safe_host = f"[{hostname}]" if ":" in hostname else hostname
        safe_netloc = safe_host if parsed.port is None else f"{safe_host}:{parsed.port}"

        parts = (parsed.path or "").split("/")
        for i in range(len(parts) - 1, -1, -1):
            if parts[i]:
                parts[i] = "***"
                break
        return f"{parsed.scheme}://{safe_netloc}{'/'.join(parts)}"
    except Exception:
        return "<redacted>"


def safe_http_error_context(resp: httpx.Response) -> str:
    """Build a sanitised excerpt of an error response for logs/alerts.

    Always safe to include in an exception message:

    * Body is only echoed for ``application/json`` and ``text/plain``
      content-types — HTML, binary, and unknown types are summarised by
      length + content-type only.
    * Text bodies are capped at 500 chars so a chatty receiver cannot
      flood operator logs.
    * Common request-id headers (``X-Request-Id``, ``X-Correlation-Id``,
      ``Request-Id``) are surfaced when present so the operator can
      correlate with the receiver's logs even when the body is omitted.

    Returns an empty string when the response carries no useful context
    (no recognised body and no request-id headers), so callers can
    cleanly skip adding a trailing separator.
    """
    parts: list[str] = []

    for header in _REQUEST_ID_HEADERS:
        value = resp.headers.get(header)
        if value:
            parts.append(f"{header.lower()}={value}")
            break

    content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type in _SAFE_BODY_CONTENT_TYPES:
        excerpt = resp.text[:_BODY_EXCERPT_MAX_CHARS]
        parts.append(f"body: {excerpt}")
    elif resp.content:
        kind = content_type or "unknown"
        parts.append(f"body: <{len(resp.content)} bytes of {kind}, omitted>")

    return " — ".join(parts)
