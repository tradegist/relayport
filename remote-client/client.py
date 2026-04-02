"""IBKR Remote Client — maintains a connection to IB Gateway.

Exposes a small HTTP API on port 5000 for placing orders.
"""

import asyncio
import hmac
import json
import logging
import os

from aiohttp import web
from ib_async import IB, LimitOrder, MarketOrder, Stock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("remote-client")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
IB_HOST = os.environ.get("IB_HOST", "ib-gateway")
TRADING_MODE = os.environ.get("TRADING_MODE", "paper")
IB_PORT = int(os.environ.get("IB_LIVE_PORT" if TRADING_MODE == "live" else "IB_PAPER_PORT", "4004"))
CLIENT_ID = 1

API_TOKEN = os.environ.get("API_TOKEN", "")

INITIAL_RETRY_DELAY = 10
MAX_RETRY_DELAY = 300
retry_delay = INITIAL_RETRY_DELAY


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------
ib = IB()


async def connect():
    global retry_delay
    while True:
        try:
            log.info("Connecting to IB Gateway at %s:%d ...", IB_HOST, IB_PORT)
            await ib.connectAsync(IB_HOST, IB_PORT, clientId=CLIENT_ID, timeout=20)
            log.info("Connected — accounts: %s", ib.managedAccounts())
            retry_delay = INITIAL_RETRY_DELAY
            return
        except Exception as exc:
            log.warning(
                "Connection failed: %s — retrying in %ds", exc, retry_delay
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)


def on_disconnect():
    log.warning("Disconnected from IB Gateway — will reconnect")
    asyncio.ensure_future(reconnect())


async def reconnect():
    await asyncio.sleep(retry_delay)
    if not ib.isConnected():
        await connect()


async def watchdog():
    """Periodically check the connection and reconnect if stale."""
    while True:
        await asyncio.sleep(30)
        if not ib.isConnected():
            log.warning("Watchdog: connection lost — reconnecting")
            await connect()


# ---------------------------------------------------------------------------
# HTTP API — order placement
# ---------------------------------------------------------------------------
API_PORT = int(os.environ.get("API_PORT", "5000"))


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Verify Bearer token on all /ibkr/ routes."""
    if request.path.startswith("/ibkr/"):
        if not API_TOKEN:
            log.error("API_TOKEN not configured — rejecting request")
            return web.json_response({"error": "Server misconfigured"}, status=500)
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {API_TOKEN}"):
            return web.json_response({"error": "Unauthorized"}, status=401)
    return await handler(request)


async def handle_order(request: web.Request) -> web.Response:
    """POST /ibkr/order — place a stock order.

    JSON body: {"quantity": int, "symbol": str, "orderType": "MKT"|"LMT", "limitPrice": float?}
    Positive quantity = BUY, negative = SELL.
    Requires: Authorization: Bearer <API_TOKEN>
    """
    if not ib.isConnected():
        return web.json_response({"error": "Not connected to IB Gateway"}, status=503)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    quantity = body.get("quantity")
    symbol = body.get("symbol", "").upper().strip()
    order_type = body.get("orderType", "").upper().strip()
    limit_price = body.get("limitPrice")
    exchange = body.get("exchange", "SMART").upper().strip()
    currency = body.get("currency", "USD").upper().strip()

    if not quantity or not symbol:
        return web.json_response({"error": "quantity and symbol are required"}, status=400)

    try:
        quantity = int(quantity)
    except (ValueError, TypeError):
        return web.json_response({"error": "quantity must be an integer"}, status=400)

    if quantity == 0:
        return web.json_response({"error": "quantity cannot be zero"}, status=400)

    action = "BUY" if quantity > 0 else "SELL"
    abs_qty = abs(quantity)

    if order_type == "LMT":
        if limit_price is None:
            return web.json_response({"error": "limitPrice required for LMT orders"}, status=400)
        try:
            limit_price = float(limit_price)
        except (ValueError, TypeError):
            return web.json_response({"error": "limitPrice must be a number"}, status=400)
        order = LimitOrder(action, abs_qty, limit_price)
    elif order_type == "MKT":
        order = MarketOrder(action, abs_qty)
    else:
        return web.json_response({"error": f"Unsupported orderType: {order_type}"}, status=400)

    contract = Stock(symbol, exchange, currency)

    try:
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            return web.json_response({"error": f"Could not qualify contract for {symbol}"}, status=400)
    except Exception as exc:
        return web.json_response({"error": f"Contract qualification failed: {exc}"}, status=500)

    log.info("Placing order: %s %d %s %s%s",
             action, abs_qty, symbol, order_type,
             f" @ {limit_price}" if order_type == "LMT" else "")

    try:
        trade = ib.placeOrder(contract, order)
    except Exception as exc:
        log.error("Order placement failed: %s", exc)
        return web.json_response({"error": f"Order placement failed: {exc}"}, status=500)

    # Give IBKR a moment to acknowledge
    await asyncio.sleep(1)

    result = {
        "status": trade.orderStatus.status,
        "orderId": trade.order.orderId,
        "action": action,
        "symbol": symbol,
        "quantity": abs_qty,
        "orderType": order_type,
    }
    if order_type == "LMT":
        result["limitPrice"] = limit_price

    log.info("Order placed: %s", json.dumps(result))
    return web.json_response(result)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"connected": ib.isConnected(), "tradingMode": TRADING_MODE})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def amain():
    log.info("IBKR Remote Client starting (mode=%s)", TRADING_MODE)

    await connect()

    ib.disconnectedEvent += on_disconnect

    # Start watchdog to detect stale connections
    asyncio.ensure_future(watchdog())

    log.info("Remote client ready. Starting HTTP API on port %d ...", API_PORT)

    app = web.Application(middlewares=[auth_middleware])
    app.router.add_post("/ibkr/order", handle_order)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()

    log.info("HTTP API listening on 0.0.0.0:%d", API_PORT)

    while True:
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(amain())
