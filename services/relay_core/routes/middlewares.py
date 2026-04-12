"""Auth middleware — Bearer token verification for authenticated routes."""

import hmac
import logging
import os
from collections.abc import Awaitable, Callable

from aiohttp import web

log = logging.getLogger("routes.auth")

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

AUTH_PREFIX = "/relays"


def _get_api_token() -> str:
    return os.environ.get("API_TOKEN", "").strip()


@web.middleware
async def auth_middleware(
    request: web.Request,
    handler: _Handler,
) -> web.StreamResponse:
    """Verify Bearer token on all routes under AUTH_PREFIX."""
    if request.path.startswith(f"{AUTH_PREFIX}/"):
        api_token = _get_api_token()
        if not api_token:
            log.error("API_TOKEN not configured — rejecting request")
            return web.json_response({"error": "Server misconfigured"}, status=500)
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {api_token}"):
            return web.json_response({"error": "Unauthorized"}, status=401)
    return await handler(request)
