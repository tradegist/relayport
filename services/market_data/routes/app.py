"""HTTP application factory and server lifecycle."""

import logging
import os

from aiohttp import web

from market_data.routes.dividends import handle_dividends_upcoming
from market_data.routes.middlewares import AUTH_PREFIX, auth_middleware, error_middleware

log = logging.getLogger(__name__)


def _get_port() -> int:
    raw = os.environ.get("MD_API_PORT", "").strip()
    if not raw:
        return 8001
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"Invalid MD_API_PORT={raw!r} — must be an integer") from None


async def handle_health(request: web.Request) -> web.Response:
    """GET /health — unauthenticated liveness check."""
    return web.json_response({"status": "ok"})


def create_app() -> web.Application:
    """Build the aiohttp Application with all routes wired."""
    app = web.Application(middlewares=[error_middleware, auth_middleware])
    app.router.add_get("/health", handle_health)
    app.router.add_get(f"{AUTH_PREFIX}/dividends/upcoming", handle_dividends_upcoming)
    return app


async def start_api_server() -> None:
    """Start the HTTP server (non-blocking, runs until process exits)."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    port = _get_port()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Market data API listening on 0.0.0.0:%d", port)
