"""Debug webhook inbox — captures webhook payloads for inspection."""

import json
import logging
import os
from datetime import UTC, datetime

from aiohttp import web

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("debug-webhook")

HTTP_PORT = 9000

PayloadEntry = dict[str, object]

_debug_path_key = web.AppKey("debug_path", str)
_max_payloads_key = web.AppKey("max_payloads", int)
_inbox_key = web.AppKey("inbox", list)


def _parse_max_payloads() -> int:
    raw = os.environ.get("MAX_DEBUG_WEBHOOK_PAYLOADS", "100")
    try:
        return min(int(raw), 150)
    except ValueError:
        raise SystemExit(
            f"Invalid MAX_DEBUG_WEBHOOK_PAYLOADS={raw!r} — must be an integer"
        ) from None


def _path_matches(request: web.Request) -> bool:
    path = request.match_info.get("path", "")
    debug_path: str = request.app[_debug_path_key]
    if not debug_path or path != debug_path:
        raise web.HTTPNotFound()
    return True


async def handle_post(request: web.Request) -> web.Response:
    """Capture incoming webhook payload and headers."""
    _path_matches(request)

    try:
        payload = await request.json()
    except Exception:
        payload = (await request.read()).decode("utf-8", errors="replace")

    headers = dict(request.headers)

    entry: PayloadEntry = {
        "payload": payload,
        "headers": headers,
        "received_at": datetime.now(UTC).isoformat(),
    }

    inbox: list[PayloadEntry] = request.app[_inbox_key]
    max_payloads: int = request.app[_max_payloads_key]
    inbox.append(entry)
    while len(inbox) > max_payloads:
        inbox.pop(0)

    log.info("Captured webhook payload (%d/%d stored)", len(inbox), max_payloads)
    log.debug("Entry:\n%s", json.dumps(entry, indent=2, default=str))

    return web.json_response({"payload": payload, "headers": headers})


async def handle_get(request: web.Request) -> web.Response:
    """Return all stored payloads."""
    _path_matches(request)
    inbox: list[PayloadEntry] = request.app[_inbox_key]
    return web.json_response({"payloads": inbox, "count": len(inbox)})


async def handle_delete(request: web.Request) -> web.Response:
    """Clear all stored payloads."""
    _path_matches(request)
    inbox: list[PayloadEntry] = request.app[_inbox_key]
    inbox.clear()
    log.info("Debug webhook inbox cleared")
    return web.json_response({"cleared": True})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response(
        {"status": "ok", "debug_path_configured": bool(request.app[_debug_path_key])}
    )


def create_app() -> web.Application:
    app = web.Application()
    app[_debug_path_key] = os.environ.get("DEBUG_WEBHOOK_PATH", "")
    app[_max_payloads_key] = _parse_max_payloads()
    app[_inbox_key] = []
    app.router.add_post("/debug/webhook/{path}", handle_post)
    app.router.add_get("/debug/webhook/{path}", handle_get)
    app.router.add_delete("/debug/webhook/{path}", handle_delete)
    app.router.add_get("/health", handle_health)
    return app


if __name__ == "__main__":
    debug_path = os.environ.get("DEBUG_WEBHOOK_PATH", "")
    max_payloads = _parse_max_payloads()
    if debug_path:
        log.info(
            "Debug webhook inbox starting on port %d (path=/debug/webhook/%s, max=%d)",
            HTTP_PORT,
            debug_path,
            max_payloads,
        )
    else:
        log.info(
            "Debug webhook inbox starting on port %d (no DEBUG_WEBHOOK_PATH — all requests return 404)",
            HTTP_PORT,
        )
    web.run_app(create_app(), host="0.0.0.0", port=HTTP_PORT, print=None)
