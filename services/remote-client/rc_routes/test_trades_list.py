"""Unit tests for routes/trades_list.py — GET /ibkr/trades error paths."""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from models_remote_client import ListTradesResponse
from rc_routes import create_routes

# Patch API_TOKEN at module level in middlewares so auth passes with "test-token".
_patcher = patch("rc_routes.middlewares.API_TOKEN", "test-token")


def setUpModule() -> None:
    _patcher.start()


def tearDownModule() -> None:
    _patcher.stop()


def _make_client(connected: bool = True) -> MagicMock:
    client = MagicMock()
    type(client).is_connected = PropertyMock(return_value=connected)
    client.trades = MagicMock()
    client.trades.list = AsyncMock(
        return_value=ListTradesResponse(trades=[])
    )
    return client


class TestTradesNotConnected(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        return create_routes(_make_client(connected=False))

    async def test_not_connected_returns_503(self) -> None:
        resp = await self.client.get(
            "/ibkr/trades",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 503
        body = await resp.json()
        assert "Not connected" in body["error"]


class TestTradesConnected(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        return create_routes(_make_client(connected=True))

    async def test_returns_empty_trades(self) -> None:
        resp = await self.client.get(
            "/ibkr/trades",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"trades": []}

    async def test_requires_auth(self) -> None:
        resp = await self.client.get("/ibkr/trades")
        assert resp.status == 401
