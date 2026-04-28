"""Authenticated Kraken REST API client."""

import base64
import hashlib
import hmac
import logging
import threading
import time
import urllib.parse
from typing import Any

import httpx

log = logging.getLogger(__name__)

_BASE_URL = "https://api.kraken.com"


class KrakenClient:
    """Synchronous Kraken REST API client with HMAC-SHA512 authentication."""

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._api_key = api_key
        try:
            self._api_secret_decoded: bytes = base64.b64decode(api_secret, validate=True)
        except Exception as exc:
            raise RuntimeError(
                f"KRAKEN_API_SECRET is not valid base64: {exc}"
            ) from exc
        # Kraken tracks the highest nonce ever seen for an API key and
        # rejects any later request with a lower nonce as EAPI:Invalid
        # nonce. Holding the lock around nonce generation + send keeps
        # Kraken's view strictly monotonic when the poller and listener
        # fire concurrently, and the _last_nonce floor protects against
        # NTP backwards-jumps and microsecond-resolution collisions.
        # RLock so _request() can serialize the full nonce-sign-send path
        # while _next_nonce() enforces synchronization internally as well.
        self._request_lock = threading.RLock()
        self._last_nonce = 0

    def _get_secret(self) -> bytes:
        return self._api_secret_decoded

    def _next_nonce(self) -> int:
        """Return a strictly-increasing nonce, safe to call without pre-acquiring the lock."""
        with self._request_lock:
            candidate = int(time.time() * 1_000_000)
            if candidate <= self._last_nonce:
                candidate = self._last_nonce + 1
            self._last_nonce = candidate
            return candidate

    def _sign(self, urlpath: str, data: dict[str, str | int]) -> str:
        """Compute API-Sign header value."""
        encoded = urllib.parse.urlencode(data)
        nonce = str(data["nonce"])
        msg = (nonce + encoded).encode()
        sha256_hash = hashlib.sha256(msg).digest()
        hmac_msg = urlpath.encode() + sha256_hash
        signature = hmac.new(self._get_secret(), hmac_msg, hashlib.sha512).digest()
        return base64.b64encode(signature).decode()

    def _request(self, urlpath: str, extra_data: dict[str, str | int] | None = None) -> dict[str, Any]:
        """Make an authenticated POST request to a private Kraken endpoint."""
        with self._request_lock:
            data: dict[str, str | int] = {"nonce": self._next_nonce()}
            if extra_data:
                data.update(extra_data)

            sig = self._sign(urlpath, data)
            headers = {
                "API-Key": self._api_key,
                "API-Sign": sig,
            }

            url = f"{_BASE_URL}{urlpath}"
            resp = httpx.post(url, data=data, headers=headers, timeout=15)
        resp.raise_for_status()

        try:
            body = resp.json()
        except Exception as exc:
            raise RuntimeError(
                f"Kraken API returned invalid JSON on {urlpath} "
                f"(status {resp.status_code}): {exc}"
            ) from exc

        if not isinstance(body, dict):
            raise RuntimeError(
                f"Kraken API returned unexpected JSON type on {urlpath} "
                f"(status {resp.status_code}; expected object, got {type(body).__name__})"
            )

        errors = body.get("error", [])
        if errors:
            raise RuntimeError(f"Kraken API error on {urlpath}: {errors}")

        result = body.get("result", {})
        if not isinstance(result, dict):
            raise RuntimeError(
                f"Kraken API returned unexpected result type on {urlpath} "
                f"(status {resp.status_code}; expected object, got {type(result).__name__})"
            )
        return result

    def get_trades_history(
        self,
        start: int | None = None,
        ofs: int = 0,
    ) -> dict[str, Any]:
        """Fetch trade history from Kraken.

        Args:
            start: Unix timestamp to start from (exclusive).
            ofs: Result offset for pagination.

        Returns:
            Dict with 'trades' (dict of txid -> trade info) and 'count'.
        """
        extra: dict[str, str | int] = {"ofs": ofs}
        if start is not None:
            extra["start"] = start
        return self._request("/0/private/TradesHistory", extra)

    def get_ws_token(self) -> str:
        """Obtain a short-lived WebSocket authentication token.

        Calls the ``GetWebSocketsToken`` private endpoint and returns
        the opaque token string for WS v2 subscription auth.
        """
        result = self._request("/0/private/GetWebSocketsToken")
        if not isinstance(result, dict) or "token" not in result:
            raise RuntimeError(
                "GetWebSocketsToken returned unexpected payload: "
                f"{result!r:.200}"
            )
        token = result["token"]
        if not isinstance(token, str) or not token:
            raise RuntimeError(
                f"GetWebSocketsToken returned invalid token value: {token!r}"
            )
        log.debug("Obtained Kraken WebSocket token")
        return token
