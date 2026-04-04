"""Authentication middleware for the IBKR Remote Client API."""

import hmac
import logging
import os

from aiohttp import web
from aiohttp.typedefs import Handler

log = logging.getLogger("routes")

API_TOKEN = os.environ.get("API_TOKEN", "")


@web.middleware
async def auth_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Verify Bearer token on all /ibkr/ routes."""
    if request.path.startswith("/ibkr/"):
        if not API_TOKEN:
            log.error("API_TOKEN not configured — rejecting request")
            return web.json_response({"error": "Server misconfigured"}, status=500)
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {API_TOKEN}"):
            return web.json_response({"error": "Unauthorized"}, status=401)
    return await handler(request)
