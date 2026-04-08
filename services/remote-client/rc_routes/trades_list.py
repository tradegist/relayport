"""GET /ibkr/trades — list session and completed trades."""

from aiohttp import web

from client import IBClient


async def handle_list_trades(request: web.Request) -> web.Response:
    from rc_routes import client_key

    client: IBClient = request.app[client_key]

    if not client.is_connected:
        return web.json_response(
            {"error": "Not connected to IB Gateway"}, status=503
        )

    result = await client.trades.list()
    return web.json_response(result.model_dump())
