import unittest
import unittest.mock
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from market_data.adapters import MarketDataAdapter, _instances, _registry
from market_data.models.dividends import DividendsUpcomingItem, TickerError
from market_data.routes.app import create_app

_ENV = {"MD_API_TOKEN": "test-token"}

_AAPL_ITEM = DividendsUpcomingItem(
    ex_div_date="2026-02-15",
    payment_date="2026-03-08",
    dps=1.5,
    annual_dps=1.5,
    are_dates_estimated=False,
)

_BAD_ERROR = TickerError(code="FETCH_FAILED", message="fetch failed")


class _StubAdapter(MarketDataAdapter):
    def __init__(
        self,
        data: dict[str, DividendsUpcomingItem],
        errors: dict[str, TickerError],
    ) -> None:
        self._data = data
        self._errors = errors

    def get_dividends_upcoming(
        self, symbols: list[str]
    ) -> tuple[dict[str, DividendsUpcomingItem], dict[str, TickerError]]:
        return self._data, self._errors


def _stub_adapter_factory(
    data: dict[str, DividendsUpcomingItem],
    errors: dict[str, TickerError],
) -> type[MarketDataAdapter]:
    class _Factory(_StubAdapter):
        def __init__(self) -> None:
            super().__init__(data, errors)

    return _Factory


class TestDividendsUpcomingHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._orig_registry = dict(_registry)
        self._orig_instances = dict(_instances)

    def tearDown(self) -> None:
        _registry.clear()
        _registry.update(self._orig_registry)
        _instances.clear()
        _instances.update(self._orig_instances)

    async def _get(self, url: str, token: str = "test-token") -> tuple[int, Any]:
        async with TestClient(TestServer(create_app())) as client:
            with unittest.mock.patch.dict("os.environ", _ENV):
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
                body = await resp.json()
            return resp.status, body

    # ── Routing errors ───────────────────────────────────────────────

    async def test_unknown_path_returns_json_404(self) -> None:
        async with TestClient(TestServer(create_app())) as client:
            with unittest.mock.patch.dict("os.environ", _ENV):
                resp = await client.get("/no/such/path")
                body = await resp.json()
        self.assertEqual(resp.status, 404)
        self.assertRegex(body["error"], r"\[404\]$")

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
            ) -> tuple[dict[str, DividendsUpcomingItem], dict[str, TickerError]]:
                captured.append(symbols)
                return {}, {}

        _registry["yahoo"] = _CapturingAdapter

        await self._get("/v1/market-data/dividends/upcoming?symbol=aapl,goog&target=yahoo")

        self.assertEqual(captured[0], ["AAPL", "GOOG"])

    async def test_repeated_symbol_params_accepted(self) -> None:
        captured: list[list[str]] = []

        class _CapturingAdapter(MarketDataAdapter):
            def get_dividends_upcoming(
                self, symbols: list[str]
            ) -> tuple[dict[str, DividendsUpcomingItem], dict[str, TickerError]]:
                captured.append(symbols)
                return {}, {}

        _registry["yahoo"] = _CapturingAdapter

        await self._get(
            "/v1/market-data/dividends/upcoming?symbol=AAPL&symbol=MSFT&target=yahoo"
        )

        self.assertEqual(captured[0], ["AAPL", "MSFT"])

    async def test_mixed_comma_and_repeated_params_split_correctly(self) -> None:
        captured: list[list[str]] = []

        class _CapturingAdapter(MarketDataAdapter):
            def get_dividends_upcoming(
                self, symbols: list[str]
            ) -> tuple[dict[str, DividendsUpcomingItem], dict[str, TickerError]]:
                captured.append(symbols)
                return {}, {}

        _registry["yahoo"] = _CapturingAdapter

        # ?symbol=AAPL,MSFT&symbol=GOOG — first param contains a comma
        await self._get(
            "/v1/market-data/dividends/upcoming?symbol=AAPL,MSFT&symbol=GOOG&target=yahoo"
        )

        self.assertEqual(captured[0], ["AAPL", "MSFT", "GOOG"])

    async def test_blank_symbol_returns_422(self) -> None:
        status, _ = await self._get("/v1/market-data/dividends/upcoming?symbol=,&target=yahoo")
        self.assertEqual(status, 422)

    async def test_duplicate_symbols_deduplicated(self) -> None:
        captured: list[list[str]] = []

        class _CapturingAdapter(MarketDataAdapter):
            def get_dividends_upcoming(
                self, symbols: list[str]
            ) -> tuple[dict[str, DividendsUpcomingItem], dict[str, TickerError]]:
                captured.append(symbols)
                return {}, {}

        _registry["yahoo"] = _CapturingAdapter

        await self._get(
            "/v1/market-data/dividends/upcoming?symbol=AAPL,AAPL,MSFT&target=yahoo"
        )

        self.assertEqual(captured[0], ["AAPL", "MSFT"])

    async def test_too_many_symbols_returns_422(self) -> None:
        symbols = ",".join(f"T{i:03d}" for i in range(21))
        status, _ = await self._get(
            f"/v1/market-data/dividends/upcoming?symbol={symbols}&target=yahoo"
        )
        self.assertEqual(status, 422)

    async def test_partial_error_returns_both_data_and_errors(self) -> None:
        _registry["yahoo"] = _stub_adapter_factory({"AAPL": _AAPL_ITEM}, {"BAD": _BAD_ERROR})

        status, body = await self._get(
            "/v1/market-data/dividends/upcoming?symbol=AAPL,BAD&target=yahoo"
        )
        self.assertEqual(status, 200)
        self.assertIn("AAPL", body["data"])
        self.assertEqual(body["errors"]["BAD"]["code"], "FETCH_FAILED")
        self.assertEqual(body["errors"]["BAD"]["message"], "fetch failed")
