"""Authentication middleware for the IBKR Remote Client API."""

import hmac
import logging
import os

from aiohttp import web
from aiohttp.typedefs import Handler

log = logging.getLogger("routes")


def get_api_token() -> str:
    return os.environ.get("API_TOKEN", "").strip()


@web.middleware
async def auth_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Verify Bearer token on all /ibkr/ routes."""
    if request.path.startswith("/ibkr/"):
        api_token = get_api_token()
        if not api_token:
            log.error("API_TOKEN not configured — rejecting request")
            return web.json_response({"error": "Server misconfigured"}, status=500)
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {api_token}"):
            return web.json_response({"error": "Unauthorized"}, status=401)
    return await handler(request)
