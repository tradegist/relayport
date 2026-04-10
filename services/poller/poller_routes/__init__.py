"""Routes package — assembles middleware, handlers, and route table."""

import asyncio
import logging
import os

from aiohttp import web

from notifier.base import BaseNotifier
from poller_routes.health import handle_health
from poller_routes.middlewares import auth_middleware
from poller_routes.run import handle_run_poll

log = logging.getLogger("poller")


def get_poller_api_port() -> int:
    return int(os.environ.get("POLLER_API_PORT", "8000"))


def create_routes(
    poll_lock: asyncio.Lock,
    notifiers: list[BaseNotifier] | None = None,
) -> web.Application:
    """Create and return the aiohttp Application with all routes wired."""
    app = web.Application(middlewares=[auth_middleware])
    app["poll_lock"] = poll_lock
    app["notifiers"] = notifiers or []
    app.router.add_get("/health", handle_health)
    app.router.add_post("/ibkr/poller/run", handle_run_poll)
    return app


async def start_api_server(
    poll_lock: asyncio.Lock,
    notifiers: list[BaseNotifier] | None = None,
) -> None:
    app = create_routes(poll_lock, notifiers)
    runner = web.AppRunner(app)
    await runner.setup()
    api_port = get_poller_api_port()
    site = web.TCPSite(runner, "0.0.0.0", api_port)
    await site.start()
    log.info("Poll API listening on 0.0.0.0:%d", api_port)
