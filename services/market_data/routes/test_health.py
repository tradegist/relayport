import unittest
import unittest.mock

from aiohttp.test_utils import TestClient, TestServer

from market_data.routes.app import create_app

_ENV = {"MD_API_TOKEN": "test-token"}


class TestHealthEndpoints(unittest.IsolatedAsyncioTestCase):
    async def test_health_is_unauthenticated(self) -> None:
        async with TestClient(TestServer(create_app())) as client:
            resp = await client.get("/health")
            body = await resp.json()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body["status"], "ok")

    async def test_public_health_is_unauthenticated(self) -> None:
        async with TestClient(TestServer(create_app())) as client:
            with unittest.mock.patch.dict("os.environ", _ENV):
                resp = await client.get("/v1/market-data/health")
                body = await resp.json()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body["status"], "ok")
