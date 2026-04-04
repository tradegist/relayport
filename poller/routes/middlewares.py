"""Authentication middleware for the poller HTTP API."""

import hmac
import logging
import os
from collections.abc import Callable, Awaitable

from aiohttp import web

log = logging.getLogger("poller")

API_TOKEN = os.environ.get("API_TOKEN", "")

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@web.middleware
async def auth_middleware(
    request: web.Request,
    handler: _Handler,
) -> web.StreamResponse:
    """Verify Bearer token on all /ibkr/ routes."""
    if request.path.startswith("/ibkr/"):
        if not API_TOKEN:
            log.error("API_TOKEN not configured — rejecting request")
            return web.json_response({"error": "Server misconfigured"}, status=500)
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {API_TOKEN}"):
            return web.json_response({"error": "Unauthorized"}, status=401)
    return await handler(request)
