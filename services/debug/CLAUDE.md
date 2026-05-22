# `services/debug/` — Debug webhook inbox

Standalone aiohttp container that captures webhook payloads for inspection during development.

## Endpoints

- `POST /debug/webhook/{path}` — captures a payload.
- `GET /debug/webhook/{path}` — returns all stored payloads.
- `DELETE /debug/webhook/{path}` — clears the inbox.
- `GET /health` — health check.

## Behaviour

- **`DEBUG_WEBHOOK_PATH`** env var controls the accepted path segment. Requests to any other path return 404. When unset, the container is not running (`DEBUG_REPLICAS=0`).
- **In-memory inbox** — `_inbox: list[PayloadEntry]` stores payloads + headers + timestamp. Capped at `MAX_DEBUG_WEBHOOK_PAYLOADS` (default 100, hard max 150) with FIFO eviction.
- **Logging** — summary at INFO, full payload+headers at DEBUG. Set `DEBUG_LOG_LEVEL=DEBUG` in `.env` and `docker logs -f debug` to tail. Aggressive log rotation (`max-size: 10k`, `max-file: 1`) keeps disk usage minimal.
- **No auth** — the debug path in the URL acts as a shared secret. The service is not exposed to the internet unless Caddy routes to it via `debug.caddy`.
- **Port 9000** is hardcoded (`HTTP_PORT = 9000`). Caddy reverse-proxies to `debug:9000` in production. Local dev: `15003:9000`. E2E: `15012:9000`.
- **Module name**: `debug_app.py` (not `main.py`) to avoid `sys.modules` collisions when both are on `sys.path`.
