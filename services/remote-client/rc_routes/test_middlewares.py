"""Unit tests for auth middleware."""

import os
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from rc_routes.middlewares import auth_middleware


async def _ok_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


class TestAuthMiddleware(AioHTTPTestCase):
    """Auth middleware rejects requests when API_TOKEN is empty or wrong."""

    async def get_application(self) -> web.Application:
        app = web.Application(middlewares=[auth_middleware])
        app.router.add_get("/health", _ok_handler)
        app.router.add_get("/ibkr/order", _ok_handler)
        return app

    @patch.dict(os.environ, {"API_TOKEN": "test-token"})
    async def test_health_bypasses_auth(self) -> None:
        resp = await self.client.request("GET", "/health")
        self.assertEqual(resp.status, 200)

    @patch.dict(os.environ, {"API_TOKEN": "test-token"})
    async def test_missing_auth_header(self) -> None:
        resp = await self.client.request("GET", "/ibkr/order")
        self.assertEqual(resp.status, 401)

    @patch.dict(os.environ, {"API_TOKEN": "test-token"})
    async def test_invalid_token(self) -> None:
        resp = await self.client.request(
            "GET", "/ibkr/order",
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(resp.status, 401)

    @patch.dict(os.environ, {"API_TOKEN": "valid-token"})
    async def test_valid_token(self) -> None:
        resp = await self.client.request(
            "GET", "/ibkr/order",
            headers={"Authorization": "Bearer valid-token"},
        )
        self.assertEqual(resp.status, 200)

    @patch.dict(os.environ, {"API_TOKEN": ""})
    async def test_empty_api_token_rejects_all(self) -> None:
        """Empty API_TOKEN must return 500, not silently accept empty Bearer."""
        resp = await self.client.request(
            "GET", "/ibkr/order",
            headers={"Authorization": "Bearer "},
        )
        self.assertEqual(resp.status, 500)
        body = await resp.json()
        self.assertIn("misconfigured", body["error"])

    @patch.dict(os.environ, {"API_TOKEN": ""})
    async def test_empty_api_token_health_still_works(self) -> None:
        """Health endpoint works even when API_TOKEN is empty."""
        resp = await self.client.request("GET", "/health")
        self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
