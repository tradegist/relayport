"""GET /health — poller status."""

from aiohttp import web

from models_poller import HealthResponse


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response(HealthResponse(status="ok").model_dump())
