"""HTTP routes — health check and per-relay on-demand poll.

Routes:
    GET  /health                                — unauthenticated status check
    POST /relays/{relay_name}/poll/{poll_idx}   — authenticated on-demand poll
"""

import asyncio
import logging
import os

from aiohttp import web

from .. import BrokerRelay
from ..poller_engine import poll_once
from ..relay_models import HealthResponse, RunPollResponse
from .middlewares import AUTH_PREFIX, auth_middleware

log = logging.getLogger("routes")

_RELAYS_KEY: web.AppKey[dict[str, BrokerRelay]] = web.AppKey("relays")


async def handle_health(request: web.Request) -> web.Response:
    """GET /health — unauthenticated status check."""
    resp = HealthResponse(status="ok")
    return web.json_response(resp.model_dump())


async def handle_poll(request: web.Request) -> web.Response:
    """POST /relays/{relay_name}/poll/{poll_idx} — trigger an on-demand poll."""
    relay_name = request.match_info["relay_name"]
    poll_idx_raw = request.match_info["poll_idx"]
    relays: dict[str, BrokerRelay] = request.app[_RELAYS_KEY]

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
            {"error": f"Invalid poll index: {poll_idx_raw!r} (must be a positive integer)"}, status=400,
        )

    if poll_idx < 0 or poll_idx >= len(relay.poller_configs):
        n = len(relay.poller_configs)
        return web.json_response(
            {"error": f"Poller {poll_idx_raw} not configured "
             f"(relay {relay_name!r} has {n}, use 1–{n})"}, status=404,
        )

    # Parse optional replay count from body
    replay = 0
    if request.body_exists:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": "Request body must be valid JSON"}, status=400,
            )
        if not isinstance(body, dict):
            return web.json_response(
                {"error": "Request body must be a JSON object"}, status=400,
            )
        raw_replay = body.get("replay")
        if raw_replay is not None:
            try:
                replay = int(raw_replay)
            except (TypeError, ValueError):
                return web.json_response(
                    {"error": f"Invalid replay value: {raw_replay!r}"}, status=400,
                )
            if replay < 0:
                return web.json_response(
                    {"error": "replay must be >= 0"}, status=400,
                )

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
            poller_index=poll_idx,
            replay=replay,
        )

        resp = RunPollResponse(trades=trades)
        return web.json_response(resp.model_dump())
    except Exception:
        log.exception("On-demand poll failed for relay %s poller %s", relay_name, poll_idx_raw)
        return web.json_response({"error": "Internal server error"}, status=500)
    finally:
        if poll_lock is not None:
            poll_lock.release()


# ── App factory ──────────────────────────────────────────────────────


def get_api_port() -> int:
    """Read API_PORT from env (default 8000)."""
    raw = os.environ.get("API_PORT", "").strip()
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
    app[_RELAYS_KEY] = relay_map

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
