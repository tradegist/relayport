"""POST /ibkr/order — place an order."""

import logging

from aiohttp import web
from pydantic import ValidationError

from client import IBClient
from models_remote_client import PlaceOrderPayload

log = logging.getLogger("routes")


async def handle_order(request: web.Request) -> web.Response:
    from rc_routes import client_key

    client: IBClient = request.app[client_key]

    if not client.is_connected:
        return web.json_response(
            {"error": "Not connected to IB Gateway"}, status=503
        )

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    try:
        req = PlaceOrderPayload.model_validate(body)
    except ValidationError as exc:
        return web.json_response(
            {"error": exc.errors(include_url=False)}, status=400
        )

    try:
        result = await client.orders.place(
            contract_req=req.contract,
            order_req=req.order,
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except RuntimeError as exc:
        log.error("Order failed: %s", exc)
        return web.json_response({"error": str(exc)}, status=500)

    response = result.model_dump(exclude_none=True)
    log.info("Order placed: %s", response)
    return web.json_response(response)
