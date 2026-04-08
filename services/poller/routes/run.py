"""POST /ibkr/poller/run — trigger an on-demand poll."""

import asyncio
import logging

from aiohttp import web

from models_poller import RunPollResponse
from poller import poll_once

log = logging.getLogger("poller")


async def handle_run_poll(request: web.Request) -> web.Response:
    poll_lock: asyncio.Lock = request.app["poll_lock"]
    notifiers = request.app["notifiers"]

    # Parse optional overrides from body
    flex_token = None
    flex_query_id = None
    replay = 0
    try:
        body = await request.json()
        flex_token = body.get("ibkr_flex_token") or None
        flex_query_id = body.get("ibkr_flex_query_id") or None
        replay = int(body.get("replay") or 0)
    except Exception:
        pass  # no body or malformed — use env defaults

    try:
        await asyncio.wait_for(poll_lock.acquire(), timeout=0)
    except TimeoutError:
        return web.json_response({"error": "Poll already in progress"}, status=409)

    try:
        trades = await asyncio.to_thread(
            poll_once,
            flex_token=flex_token, flex_query_id=flex_query_id, replay=replay,
            notifiers=notifiers,
        )
        result = trades if isinstance(trades, list) else []
        resp = RunPollResponse(trades=result)
        return web.json_response(resp.model_dump())
    except Exception as exc:
        log.exception("On-demand poll failed")
        return web.json_response({"error": str(exc)}, status=500)
    finally:
        poll_lock.release()
