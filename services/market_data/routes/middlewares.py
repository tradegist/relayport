"""Route middlewares — error handling and Bearer token auth."""

import hmac
import logging
import os
from collections.abc import Awaitable, Callable

from aiohttp import web

from market_data.errors import AppError, ErrorCode, UserError

log = logging.getLogger(__name__)

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

AUTH_PREFIX = "/v1/market-data"
_PUBLIC_PATHS = frozenset({f"{AUTH_PREFIX}/health"})


@web.middleware
async def error_middleware(request: web.Request, handler: _Handler) -> web.StreamResponse:
    """Catch AppError/UserError and unexpected exceptions; return structured JSON."""
    try:
        return await handler(request)
    except web.HTTPException as exc:
        return web.json_response({"error": f"{exc.reason} [{exc.status}]"}, status=exc.status)
    except AppError as exc:
        if isinstance(exc, UserError):
            log.warning("User error on %s %s: %s", request.method, request.path, exc)
            return web.json_response({"error": str(exc)}, status=exc.status_code)
        else:
            log.error("App error on %s %s: %s", request.method, request.path, exc)
            return web.json_response(
                {"error": f"Internal server error [{ErrorCode.INTERNAL_ERROR}]"}, status=exc.status_code
            )
    except Exception:
        log.exception("Unhandled exception on %s %s", request.method, request.path)
        return web.json_response(
            {"error": f"Internal server error [{ErrorCode.INTERNAL_ERROR}]"}, status=500
        )


def _get_api_token() -> str:
    return os.environ.get("MD_API_TOKEN", "").strip()


def validate_api_token() -> None:
    """Raise SystemExit if MD_API_TOKEN is missing — call once at startup."""
    if not _get_api_token():
        raise SystemExit("MD_API_TOKEN must be set")


@web.middleware
async def auth_middleware(
    request: web.Request,
    handler: _Handler,
) -> web.StreamResponse:
    """Verify Bearer token on all routes under AUTH_PREFIX."""
    if request.path.startswith(f"{AUTH_PREFIX}/") and request.path not in _PUBLIC_PATHS:
        api_token = _get_api_token()
        if not api_token:
            raise AppError("MD_API_TOKEN not configured", ErrorCode.INTERNAL_ERROR)
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {api_token}"):
            raise UserError("Unauthorized", ErrorCode.UNAUTHORIZED)
    return await handler(request)
