"""GET /health — poller status."""

from aiohttp import web


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})
