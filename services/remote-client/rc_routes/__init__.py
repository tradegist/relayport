"""Routes package — assembles middleware, handlers, and route table."""

from aiohttp import web

from client import IBClient
from rc_routes.health import handle_health
from rc_routes.middlewares import auth_middleware
from rc_routes.order_place import handle_order
from rc_routes.trades_list import handle_list_trades

client_key: web.AppKey[IBClient] = web.AppKey("client", IBClient)


def create_routes(client: IBClient) -> web.Application:
    """Create and return the aiohttp Application with all routes wired."""
    app = web.Application(middlewares=[auth_middleware])
    app[client_key] = client
    app.router.add_post("/ibkr/order", handle_order)
    app.router.add_get("/ibkr/trades", handle_list_trades)
    app.router.add_get("/health", handle_health)
    return app
