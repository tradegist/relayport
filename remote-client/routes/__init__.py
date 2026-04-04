"""Routes package — assembles middleware, handlers, and route table."""

from aiohttp import web

from client import IBClient
from routes.health import handle_health
from routes.middlewares import auth_middleware
from routes.order_place import handle_order


def create_routes(client: IBClient) -> web.Application:
    """Create and return the aiohttp Application with all routes wired."""
    app = web.Application(middlewares=[auth_middleware])
    app["client"] = client
    app.router.add_post("/ibkr/order", handle_order)
    app.router.add_get("/health", handle_health)
    return app
