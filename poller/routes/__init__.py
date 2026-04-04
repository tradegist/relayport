"""Routes package — assembles middleware, handlers, and route table."""

import asyncio
import logging
import os
import sqlite3

from aiohttp import web

from routes.health import handle_health
from routes.middlewares import auth_middleware
from routes.run import handle_run_poll

log = logging.getLogger("poller")

API_PORT = int(os.environ.get("POLLER_API_PORT", "8000"))


def create_routes(
    db_conn: sqlite3.Connection,
    poll_lock: asyncio.Lock,
) -> web.Application:
    """Create and return the aiohttp Application with all routes wired."""
    app = web.Application(middlewares=[auth_middleware])
    app["db_conn"] = db_conn
    app["poll_lock"] = poll_lock
    app.router.add_get("/health", handle_health)
    app.router.add_post("/ibkr/poller/run", handle_run_poll)
    return app


async def start_api_server(
    db_conn: sqlite3.Connection,
    poll_lock: asyncio.Lock,
) -> None:
    app = create_routes(db_conn, poll_lock)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()
    log.info("Poll API listening on 0.0.0.0:%d", API_PORT)
