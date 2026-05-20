import json
import unittest
import unittest.mock

from market_data.yahoo_client import YahooClient
from market_data.yahoo_client.dividends import fetch_dividend_info_from_yahoo, fetch_with_retry
from market_data.yahoo_client.types import DividendInfo, YahooSession

_FROZEN_NOW = 1768435200.0  # 2026-01-15T00:00:00Z

_MOCK_SESSION = YahooSession(cookie_string="A3=test-cookie", crumb="test-crumb-abc")

# Quarterly timestamps exactly 90 days apart, all before the frozen now.
# last known: 2025-10-17 (unix 1760659200)
# avg gap: 90 days → estimated ex-div: 2026-04-15, payment: 2026-05-06 (21d offset)
_QUARTERLY_TIMESTAMPS = [1737331200, 1745107200, 1752883200, 1760659200]
_ESTIMATED_EX_DIV = "2026-04-15"
_ESTIMATED_PAYMENT = "2026-05-06"


# ─── helpers ─────────────────────────────────────────────────────────────────


def _make_response(
    status_code: int,
    body: str,
    cookies: list[str] | None = None,
) -> unittest.mock.MagicMock:
    resp = unittest.mock.MagicMock()
    resp.status_code = status_code
    resp.text = body
    if body:
        try:
            resp.json.return_value = json.loads(body)
        except json.JSONDecodeError:
            resp.json.side_effect = json.JSONDecodeError("Not JSON", body, 0)
    else:
        resp.json.return_value = {}
    resp.cookies.items.return_value = [
        (c.split("=", 1)[0], c.split("=", 1)[1]) for c in (cookies or [])
    ]
    return resp


def _make_summary_body(
    ex_div_unix: int | None,
    payment_unix: int | None,
    dps: float = 1.5,
) -> str:
    calendar: dict[str, object] = {}
    if ex_div_unix is not None:
        calendar["exDividendDate"] = {"raw": ex_div_unix}
    if payment_unix is not None:
        calendar["dividendDate"] = {"raw": payment_unix}
    return json.dumps({
        "quoteSummary": {
            "result": [{
                "calendarEvents": calendar,
                "summaryDetail": {"dividendRate": {"raw": dps}},
            }]
        }
    })


def _make_chart_body(timestamps: list[int], amount_per_div: float = 0.25) -> str:
    dividends = {str(ts): {"amount": amount_per_div} for ts in timestamps}
    return json.dumps({"chart": {"result": [{"events": {"dividends": dividends}}]}})


def _make_mock_client(responses: list[unittest.mock.MagicMock]) -> unittest.mock.MagicMock:
    mock_client = unittest.mock.MagicMock()
    mock_client.__enter__ = unittest.mock.Mock(return_value=mock_client)
    mock_client.__exit__ = unittest.mock.Mock(return_value=False)
    mock_client.get.side_effect = responses
    return mock_client


# ─── fetchDividendInfoFromYahoo ───────────────────────────────────────────────


class TestFetchDividendInfoFromYahoo(unittest.TestCase):
    def test_happy_path_returns_announced_future_dates(self) -> None:
        future_ex_div = 1771113600  # 2026-02-15T00:00:00Z (after frozen now 2026-01-15)
        future_payment = 1772928000  # 2026-03-08T00:00:00Z

        mock_client = _make_mock_client([
            _make_response(200, _make_summary_body(future_ex_div, future_payment, 1.5)),
        ])
        with unittest.mock.patch("time.time", return_value=_FROZEN_NOW), \
             unittest.mock.patch("market_data.yahoo_client.dividends.cffi_requests.Session", return_value=mock_client):
            result = fetch_dividend_info_from_yahoo("AAPL", _MOCK_SESSION)

        self.assertEqual(result, DividendInfo(
            ex_div_date="2026-02-15",
            payment_date="2026-03-08",
            dps=1.5,
            are_dates_estimated=False,
        ))
        mock_client.get.assert_called_once()

    def test_estimation_path_estimates_from_chart_history(self) -> None:
        past_unix = 1760659200  # 2025-10-17, before frozen now

        mock_client = _make_mock_client([
            _make_response(200, _make_summary_body(past_unix, past_unix, 0.25)),
            _make_response(200, _make_chart_body(_QUARTERLY_TIMESTAMPS, 0.25)),
        ])
        with unittest.mock.patch("time.time", return_value=_FROZEN_NOW), \
             unittest.mock.patch("market_data.yahoo_client.dividends.cffi_requests.Session", return_value=mock_client):
            result = fetch_dividend_info_from_yahoo("AAPL", _MOCK_SESSION)

        self.assertEqual(result, DividendInfo(
            ex_div_date=_ESTIMATED_EX_DIV,
            payment_date=_ESTIMATED_PAYMENT,
            dps=0.25,
            are_dates_estimated=True,
        ))
        self.assertEqual(mock_client.get.call_count, 2)

    def test_raises_yahoo_error_on_401(self) -> None:
        from market_data.errors import YahooError

        mock_client = _make_mock_client([_make_response(401, "")])
        with unittest.mock.patch("time.time", return_value=_FROZEN_NOW), \
             unittest.mock.patch("market_data.yahoo_client.dividends.cffi_requests.Session", return_value=mock_client), \
             self.assertRaises(YahooError) as ctx:
            fetch_dividend_info_from_yahoo("AAPL", _MOCK_SESSION)

        self.assertEqual(ctx.exception.error_code, "YAHOO_UNAUTHORIZED")

    def test_returns_null_dates_when_chart_unavailable(self) -> None:
        past_unix = 1760659200

        mock_client = _make_mock_client([
            _make_response(200, _make_summary_body(past_unix, past_unix, 1.5)),
            _make_response(404, ""),
        ])
        with unittest.mock.patch("time.time", return_value=_FROZEN_NOW), \
             unittest.mock.patch("market_data.yahoo_client.dividends.cffi_requests.Session", return_value=mock_client):
            result = fetch_dividend_info_from_yahoo("AAPL", _MOCK_SESSION)

        self.assertIsNone(result.ex_div_date)
        self.assertIsNone(result.payment_date)
        self.assertEqual(result.dps, 1.5)
        self.assertFalse(result.are_dates_estimated)

    def test_returns_null_dates_when_no_dividend_history(self) -> None:
        past_unix = 1760659200
        empty_chart = json.dumps({"chart": {"result": [{"events": {}}]}})

        mock_client = _make_mock_client([
            _make_response(200, _make_summary_body(past_unix, past_unix, 1.5)),
            _make_response(200, empty_chart),
        ])
        with unittest.mock.patch("time.time", return_value=_FROZEN_NOW), \
             unittest.mock.patch("market_data.yahoo_client.dividends.cffi_requests.Session", return_value=mock_client):
            result = fetch_dividend_info_from_yahoo("AAPL", _MOCK_SESSION)

        self.assertIsNone(result.ex_div_date)
        self.assertFalse(result.are_dates_estimated)


# ─── fetchWithRetry ───────────────────────────────────────────────────────────


class TestFetchWithRetry(unittest.TestCase):
    def test_refreshes_session_on_401_and_retries(self) -> None:
        future_ex_div = 1771113600  # 2026-02-15 (after frozen now 2026-01-15)
        future_payment = 1772928000  # 2026-03-08

        fresh_session = YahooSession(cookie_string="A3=test-cookie", crumb="test-crumb-abc")

        # First cffi_requests.Session call raises 401; session refresh is mocked directly;
        # second cffi_requests.Session call returns 200.
        first_client = _make_mock_client([_make_response(401, "")])
        second_client = _make_mock_client([
            _make_response(200, _make_summary_body(future_ex_div, future_payment, 1.5)),
        ])

        client_side_effects = [first_client, second_client]

        with unittest.mock.patch("time.time", return_value=_FROZEN_NOW), \
             unittest.mock.patch("time.sleep"), \
             unittest.mock.patch(
                 "market_data.yahoo_client.dividends.get_yahoo_session",
                 return_value=fresh_session,
             ), \
             unittest.mock.patch(
                 "market_data.yahoo_client.dividends.cffi_requests.Session",
                 side_effect=client_side_effects,
             ):
            info, _ = fetch_with_retry("AAPL", _MOCK_SESSION)

        self.assertEqual(info, DividendInfo(
            ex_div_date="2026-02-15",
            payment_date="2026-03-08",
            dps=1.5,
            are_dates_estimated=False,
        ))

    def test_does_not_retry_non_401_errors(self) -> None:
        from market_data.errors import YahooError

        mock_client = _make_mock_client([_make_response(500, "")])
        with unittest.mock.patch("time.time", return_value=_FROZEN_NOW), \
             unittest.mock.patch("market_data.yahoo_client.dividends.cffi_requests.Session", return_value=mock_client), \
             self.assertRaises(YahooError) as ctx:
            fetch_with_retry("AAPL", _MOCK_SESSION)

        self.assertIsNone(ctx.exception.error_code)


# ─── YahooClient cache integration ───────────────────────────────────────────


class TestYahooClientCache(unittest.TestCase):
    def test_returns_cached_result_without_api_calls(self) -> None:
        cached_result = DividendInfo(
            ex_div_date="2026-02-15",
            payment_date="2026-03-08",
            dps=1.5,
            are_dates_estimated=False,
        )

        client = YahooClient()
        from market_data.yahoo_client.cache import _cache_key
        from market_data.yahoo_client.types import CacheEntry
        client._cache[_cache_key("AAPL")] = CacheEntry(
            data=cached_result, cached_at=_FROZEN_NOW - 1
        )

        with unittest.mock.patch("time.time", return_value=_FROZEN_NOW), \
             unittest.mock.patch("market_data.yahoo_client.dividends.cffi_requests.Session") as mock_cls:
            data, errors = client.get_dividends_info(["AAPL"])

        self.assertEqual(data["AAPL"], cached_result)
        self.assertEqual(errors, {})
        mock_cls.assert_not_called()

    def test_fetches_fresh_data_when_cache_expired(self) -> None:
        future_ex_div = 1771113600  # 2026-02-15 (after frozen now 2026-01-15)
        future_payment = 1772928000  # 2026-03-08

        mock_httpx_client = _make_mock_client([
            _make_response(200, _make_summary_body(future_ex_div, future_payment, 1.5)),
        ])

        yahoo_client = YahooClient()

        mock_session = YahooSession(cookie_string="A3=test-cookie", crumb="test-crumb-abc")
        with unittest.mock.patch("time.time", return_value=_FROZEN_NOW), \
             unittest.mock.patch("time.sleep"), \
             unittest.mock.patch(
                 "market_data.yahoo_client.dividends.cffi_requests.Session",
                 return_value=mock_httpx_client,
             ), \
             unittest.mock.patch(
                 "market_data.yahoo_client.get_yahoo_session",
                 return_value=mock_session,
             ):
            data, errors = yahoo_client.get_dividends_info(["AAPL"])

        self.assertEqual(data["AAPL"], DividendInfo(
            ex_div_date="2026-02-15",
            payment_date="2026-03-08",
            dps=1.5,
            are_dates_estimated=False,
        ))
        self.assertEqual(errors, {})

    def test_returns_error_entry_on_fetch_failure(self) -> None:
        mock_httpx_client = _make_mock_client([_make_response(500, "")])

        yahoo_client = YahooClient()

        mock_session = YahooSession(cookie_string="A3=test-cookie", crumb="test-crumb-abc")
        with unittest.mock.patch("time.time", return_value=_FROZEN_NOW), \
             unittest.mock.patch("time.sleep"), \
             unittest.mock.patch(
                 "market_data.yahoo_client.dividends.cffi_requests.Session",
                 return_value=mock_httpx_client,
             ), \
             unittest.mock.patch(
                 "market_data.yahoo_client.get_yahoo_session",
                 return_value=mock_session,
             ):
            data, errors = yahoo_client.get_dividends_info(["AAPL"])

        self.assertEqual(data, {})
        self.assertIn("AAPL", errors)
        self.assertIsInstance(errors["AAPL"], str)


