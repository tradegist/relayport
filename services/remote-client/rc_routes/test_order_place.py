"""Unit tests for routes/order_place.py — request validation and error paths."""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from rc_routes import create_routes

# Patch API_TOKEN at module level in middlewares so auth passes with "test-token".
_patcher = patch("rc_routes.middlewares.API_TOKEN", "test-token")


def setUpModule() -> None:
    _patcher.start()


def tearDownModule() -> None:
    _patcher.stop()


def _make_client(connected: bool = True) -> MagicMock:
    """Build a mock IBClient."""
    client = MagicMock()
    type(client).is_connected = PropertyMock(return_value=connected)
    client.orders = MagicMock()
    client.orders.place = AsyncMock()
    return client


class TestOrderValidation(AioHTTPTestCase):
    """Test Pydantic validation and error handling in POST /ibkr/order."""

    async def get_application(self) -> web.Application:
        mock_client = _make_client()
        mock_response = MagicMock()
        mock_response.model_dump.return_value = {
            "status": "Submitted", "orderId": 100,
            "action": "BUY", "symbol": "AAPL", "totalQuantity": 1.0,
            "orderType": "MKT",
        }
        mock_client.orders.place = AsyncMock(return_value=mock_response)
        self.mock_client = mock_client
        return create_routes(mock_client)

    async def test_missing_symbol_returns_400(self) -> None:
        resp = await self.client.post(
            "/ibkr/order",
            json={"contract": {}, "order": {"action": "BUY", "totalQuantity": 1, "orderType": "MKT"}},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 400

    async def test_zero_quantity_returns_400(self) -> None:
        resp = await self.client.post(
            "/ibkr/order",
            json={
                "contract": {"symbol": "AAPL"},
                "order": {"action": "BUY", "totalQuantity": 0, "orderType": "MKT"},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 400

    async def test_negative_quantity_returns_400(self) -> None:
        resp = await self.client.post(
            "/ibkr/order",
            json={
                "contract": {"symbol": "AAPL"},
                "order": {"action": "BUY", "totalQuantity": -5, "orderType": "MKT"},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 400

    async def test_invalid_order_type_returns_400(self) -> None:
        resp = await self.client.post(
            "/ibkr/order",
            json={
                "contract": {"symbol": "AAPL"},
                "order": {"action": "BUY", "totalQuantity": 1, "orderType": "STOP"},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 400

    async def test_invalid_action_returns_400(self) -> None:
        resp = await self.client.post(
            "/ibkr/order",
            json={
                "contract": {"symbol": "AAPL"},
                "order": {"action": "SHORT", "totalQuantity": 1, "orderType": "MKT"},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 400

    async def test_extra_field_returns_400(self) -> None:
        resp = await self.client.post(
            "/ibkr/order",
            json={
                "contract": {"symbol": "AAPL"},
                "order": {"action": "BUY", "totalQuantity": 1, "orderType": "MKT", "foo": "bar"},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 400

    async def test_invalid_json_returns_400(self) -> None:
        resp = await self.client.post(
            "/ibkr/order",
            data=b"not json",
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["error"] == "Invalid JSON"

    async def test_valid_request_returns_200(self) -> None:
        resp = await self.client.post(
            "/ibkr/order",
            json={
                "contract": {"symbol": "AAPL"},
                "order": {"action": "BUY", "totalQuantity": 1, "orderType": "MKT"},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 200


class TestOrderNotConnected(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        return create_routes(_make_client(connected=False))

    async def test_not_connected_returns_503(self) -> None:
        resp = await self.client.post(
            "/ibkr/order",
            json={
                "contract": {"symbol": "AAPL"},
                "order": {"action": "BUY", "totalQuantity": 1, "orderType": "MKT"},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 503


class TestOrderBusinessErrors(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        mock_client = _make_client()
        self.mock_client = mock_client
        return create_routes(mock_client)

    async def test_value_error_returns_400(self) -> None:
        self.mock_client.orders.place = AsyncMock(
            side_effect=ValueError("lmtPrice required")
        )
        resp = await self.client.post(
            "/ibkr/order",
            json={
                "contract": {"symbol": "AAPL"},
                "order": {"action": "BUY", "totalQuantity": 1, "orderType": "MKT"},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 400

    async def test_runtime_error_returns_500(self) -> None:
        self.mock_client.orders.place = AsyncMock(
            side_effect=RuntimeError("IB internal error")
        )
        resp = await self.client.post(
            "/ibkr/order",
            json={
                "contract": {"symbol": "AAPL"},
                "order": {"action": "BUY", "totalQuantity": 1, "orderType": "MKT"},
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 500
