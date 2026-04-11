"""HTTP routes — health check and per-relay on-demand poll.

Routes:
    GET  /health                                — unauthenticated status check
    POST /relays/{relay_name}/poll/{poll_idx}   — authenticated on-demand poll
"""

import asyncio
import hmac
import logging
import os
from collections.abc import Awaitable, Callable

from aiohttp import web

from relay_core import BrokerRelay
from relay_core.poller_engine import poll_once

log = logging.getLogger("routes")

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


# ── Auth ─────────────────────────────────────────────────────────────

AUTH_PREFIX = "/relays"


def _get_api_token() -> str:
    return os.environ.get("API_TOKEN", "").strip()


@web.middleware
async def auth_middleware(
    request: web.Request,
    handler: _Handler,
) -> web.StreamResponse:
    """Verify Bearer token on all routes under AUTH_PREFIX."""
    if request.path.startswith(f"{AUTH_PREFIX}/"):
        api_token = _get_api_token()
        if not api_token:
            log.error("API_TOKEN not configured — rejecting request")
            return web.json_response({"error": "Server misconfigured"}, status=500)
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {api_token}"):
            return web.json_response({"error": "Unauthorized"}, status=401)
    return await handler(request)


# ── Handlers ─────────────────────────────────────────────────────────


async def handle_health(request: web.Request) -> web.Response:
    """GET /health — unauthenticated status check."""
    return web.json_response({"status": "ok"})


async def handle_poll(request: web.Request) -> web.Response:
    """POST /relays/{relay_name}/poll/{poll_idx} — trigger an on-demand poll."""
    relay_name = request.match_info["relay_name"]
    poll_idx_raw = request.match_info["poll_idx"]
    relays: dict[str, BrokerRelay] = request.app["relays"]

    relay = relays.get(relay_name)
    if relay is None:
        return web.json_response(
            {"error": f"Unknown relay: {relay_name!r}"}, status=404,
        )

    if not relay.poller_configs:
        return web.json_response(
            {"error": f"Relay {relay_name!r} has no pollers"}, status=400,
        )

    try:
        poll_idx = int(poll_idx_raw) - 1  # 1-based → 0-based
    except ValueError:
        return web.json_response(
            {"error": f"Invalid poll index: {poll_idx_raw!r}"}, status=400,
        )

    if poll_idx < 0 or poll_idx >= len(relay.poller_configs):
        return web.json_response(
            {"error": f"Poller {poll_idx_raw} not configured "
             f"(relay {relay_name!r} has {len(relay.poller_configs)})"}, status=404,
        )

    config = relay.poller_configs[poll_idx]

    # Parse optional overrides from body
    replay = 0
    try:
        body = await request.json()
        replay = int(body.get("replay") or 0)
    except Exception:
        pass

    # Acquire the per-poller lock (fail-fast if already running)
    poll_lock = relay.poll_locks[poll_idx] if relay.poll_locks else None
    if poll_lock is not None:
        try:
            await asyncio.wait_for(poll_lock.acquire(), timeout=0.01)
        except TimeoutError:
            return web.json_response(
                {"error": "Poll already in progress"}, status=409,
            )
    try:
        trades = await asyncio.to_thread(
            poll_once,
            relay_name=relay.name,
            config=config,
            notifiers=relay.notifiers,
            poller_index=poll_idx,
            replay=replay,
        )

        return web.json_response({
            "trades": [t.model_dump() for t in trades],
        })
    except Exception as exc:
        log.exception("On-demand poll failed for relay %s poller %s", relay_name, poll_idx_raw)
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        if poll_lock is not None:
            poll_lock.release()


# ── App factory ──────────────────────────────────────────────────────


def get_api_port() -> int:
    """Read API_PORT from env (default 8000)."""
    raw = os.environ.get("API_PORT", "").strip()
    if not raw:
        raw = os.environ.get("POLLER_API_PORT", "").strip()
    if not raw:
        return 8000
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(
            f"Invalid API_PORT={raw!r} — must be an integer"
        ) from None


def create_app(relays: list[BrokerRelay]) -> web.Application:
    """Build the aiohttp Application with all routes wired."""
    app = web.Application(middlewares=[auth_middleware])

    # Index relays by name for O(1) lookup in handlers.
    relay_map: dict[str, BrokerRelay] = {r.name: r for r in relays}
    app["relays"] = relay_map

    app.router.add_get("/health", handle_health)
    app.router.add_post(f"{AUTH_PREFIX}/{{relay_name}}/poll/{{poll_idx}}", handle_poll)

    return app


async def start_api_server(relays: list[BrokerRelay]) -> None:
    """Start the HTTP server (non-blocking)."""
    app = create_app(relays)
    runner = web.AppRunner(app)
    await runner.setup()
    port = get_api_port()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("API server listening on 0.0.0.0:%d", port)
