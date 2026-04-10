"""GET /health — connection status."""

from aiohttp import web

from client import IBClient, get_trading_mode
from rc_models import HealthResponse


async def handle_health(request: web.Request) -> web.Response:
    from rc_routes import client_key

    client: IBClient = request.app[client_key]
    resp = HealthResponse(
        connected=client.is_connected,
        tradingMode=get_trading_mode(),
    )
    return web.json_response(resp.model_dump())
