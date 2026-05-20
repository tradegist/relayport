import unittest
import unittest.mock
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from market_data.adapters import MarketDataAdapter, _registry
from market_data.models.dividends import DividendsUpcomingItem
from market_data.routes.app import create_app

_ENV = {"MD_API_TOKEN": "test-token"}

_AAPL_ITEM = DividendsUpcomingItem(
    ex_div_date="2026-02-15",
    payment_date="2026-03-08",
    dps=1.5,
    annual_dps=1.5,
    are_dates_estimated=False,
)


class _StubAdapter(MarketDataAdapter):
    def __init__(
        self,
        data: dict[str, DividendsUpcomingItem],
        errors: dict[str, str],
    ) -> None:
        self._data = data
        self._errors = errors

    def get_dividends_upcoming(
        self, symbols: list[str]
    ) -> tuple[dict[str, DividendsUpcomingItem], dict[str, str]]:
        return self._data, self._errors


def _stub_adapter_factory(
    data: dict[str, DividendsUpcomingItem],
    errors: dict[str, str],
) -> type[MarketDataAdapter]:
    class _Factory(_StubAdapter):
        def __init__(self) -> None:
            super().__init__(data, errors)

    return _Factory


class TestDividendsUpcomingHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig_registry = dict(_registry)

    def tearDown(self) -> None:
        _registry.clear()
        _registry.update(self._orig_registry)

    async def _get(self, url: str, token: str = "test-token") -> tuple[int, Any]:
        async with TestClient(TestServer(create_app())) as client:
            with unittest.mock.patch.dict("os.environ", _ENV):
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
                body = await resp.json()
            return resp.status, body

    # ── Auth ──────────────────────────────────────────────────────────

    async def test_missing_token_returns_401(self) -> None:
        async with TestClient(TestServer(create_app())) as client:
            with unittest.mock.patch.dict("os.environ", _ENV):
                resp = await client.get(
                    "/v1/market-data/dividends/upcoming?symbol=AAPL&target=yahoo"
                )
        self.assertEqual(resp.status, 401)

    async def test_wrong_token_returns_401(self) -> None:
        status, _ = await self._get(
            "/v1/market-data/dividends/upcoming?symbol=AAPL&target=yahoo",
            token="wrong-token",
        )
        self.assertEqual(status, 401)

    async def test_missing_md_api_token_env_returns_500(self) -> None:
        async with TestClient(TestServer(create_app())) as client:
            with unittest.mock.patch.dict("os.environ", {"MD_API_TOKEN": ""}):
                resp = await client.get(
                    "/v1/market-data/dividends/upcoming?symbol=AAPL&target=yahoo",
                    headers={"Authorization": "Bearer whatever"},
                )
        self.assertEqual(resp.status, 500)

    # ── Health ────────────────────────────────────────────────────────

    async def test_health_is_unauthenticated(self) -> None:
        async with TestClient(TestServer(create_app())) as client:
            resp = await client.get("/health")
            body = await resp.json()
        self.assertEqual(resp.status, 200)
        self.assertEqual(body["status"], "ok")

    # ── Validation ────────────────────────────────────────────────────

    async def test_missing_symbol_returns_422(self) -> None:
        status, _ = await self._get("/v1/market-data/dividends/upcoming?target=yahoo")
        self.assertEqual(status, 422)

    async def test_missing_target_returns_422(self) -> None:
        status, _ = await self._get("/v1/market-data/dividends/upcoming?symbol=AAPL")
        self.assertEqual(status, 422)

    async def test_invalid_target_returns_422(self) -> None:
        # "nonexistent" is not in Target = Literal["yahoo"] — Pydantic rejects it
        status, _ = await self._get(
            "/v1/market-data/dividends/upcoming?symbol=AAPL&target=nonexistent"
        )
        self.assertEqual(status, 422)

    # ── Happy path ────────────────────────────────────────────────────

    async def test_happy_path_returns_data(self) -> None:
        _registry["yahoo"] = _stub_adapter_factory({"AAPL": _AAPL_ITEM}, {})

        status, body = await self._get(
            "/v1/market-data/dividends/upcoming?symbol=AAPL&target=yahoo"
        )
        self.assertEqual(status, 200)
        self.assertIn("AAPL", body["data"])
        self.assertEqual(body["data"]["AAPL"]["ex_div_date"], "2026-02-15")
        self.assertEqual(body["data"]["AAPL"]["dps"], 1.5)
        self.assertEqual(body["errors"], {})

    async def test_symbols_uppercased_and_split(self) -> None:
        captured: list[list[str]] = []

        class _CapturingAdapter(MarketDataAdapter):
            def get_dividends_upcoming(
                self, symbols: list[str]
            ) -> tuple[dict[str, DividendsUpcomingItem], dict[str, str]]:
                captured.append(symbols)
                return {}, {}

        _registry["yahoo"] = _CapturingAdapter

        await self._get("/v1/market-data/dividends/upcoming?symbol=aapl,goog&target=yahoo")

        self.assertEqual(captured[0], ["AAPL", "GOOG"])

    async def test_partial_error_returns_both_data_and_errors(self) -> None:
        _registry["yahoo"] = _stub_adapter_factory(
            {"AAPL": _AAPL_ITEM},
            {"BAD": "fetch failed"},
        )

        status, body = await self._get(
            "/v1/market-data/dividends/upcoming?symbol=AAPL,BAD&target=yahoo"
        )
        self.assertEqual(status, 200)
        self.assertIn("AAPL", body["data"])
        self.assertEqual(body["errors"], {"BAD": "fetch failed"})
