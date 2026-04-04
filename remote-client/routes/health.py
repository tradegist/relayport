"""GET /health — connection status."""

from aiohttp import web

from client import IBClient


async def handle_health(request: web.Request) -> web.Response:
    client: IBClient = request.app["client"]
    from client import TRADING_MODE
    return web.json_response({
        "connected": client.is_connected,
        "tradingMode": TRADING_MODE,
    })
