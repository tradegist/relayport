import unittest
import unittest.mock

from market_data.adapters.yahoo import YahooAdapter
from market_data.yahoo_client.types import DividendInfo

_AAPL_INFO = DividendInfo(
    ex_div_date="2026-02-15",
    payment_date="2026-03-08",
    dps=1.5,
    are_dates_estimated=False,
)
_GOOG_INFO = DividendInfo(
    ex_div_date="2026-03-01",
    payment_date="2026-03-22",
    dps=0.5,
    are_dates_estimated=True,
)


class TestYahooAdapterMapping(unittest.TestCase):
    def test_maps_dividend_info_to_item_keyed_by_symbol(self) -> None:
        adapter = YahooAdapter()
        with unittest.mock.patch.object(
            adapter._client,
            "get_dividends_info",
            return_value=({"AAPL": _AAPL_INFO}, {}),
        ):
            data, errors = adapter.get_dividends_upcoming(["AAPL"])

        self.assertIn("AAPL", data)
        self.assertEqual(data["AAPL"].ex_div_date, "2026-02-15")
        self.assertEqual(data["AAPL"].dps, 1.5)
        self.assertFalse(data["AAPL"].are_dates_estimated)
        self.assertEqual(errors, {})

    def test_multiple_symbols_keyed_correctly(self) -> None:
        adapter = YahooAdapter()
        with unittest.mock.patch.object(
            adapter._client,
            "get_dividends_info",
            return_value=({"AAPL": _AAPL_INFO, "GOOG": _GOOG_INFO}, {}),
        ):
            data, errors = adapter.get_dividends_upcoming(["AAPL", "GOOG"])

        self.assertEqual(set(data.keys()), {"AAPL", "GOOG"})
        self.assertEqual(data["GOOG"].dps, 0.5)
        self.assertEqual(errors, {})

    def test_errors_passed_through_from_client(self) -> None:
        adapter = YahooAdapter()
        with unittest.mock.patch.object(
            adapter._client,
            "get_dividends_info",
            return_value=({"AAPL": _AAPL_INFO}, {"BAD": "network failure"}),
        ):
            data, errors = adapter.get_dividends_upcoming(["AAPL", "BAD"])

        self.assertIn("AAPL", data)
        self.assertEqual(errors, {"BAD": "network failure"})

    def test_returns_empty_dicts_for_empty_symbols(self) -> None:
        adapter = YahooAdapter()
        with unittest.mock.patch.object(
            adapter._client,
            "get_dividends_info",
            return_value=({}, {}),
        ):
            data, errors = adapter.get_dividends_upcoming([])

        self.assertEqual(data, {})
        self.assertEqual(errors, {})

    def test_no_symbol_field_on_item(self) -> None:
        adapter = YahooAdapter()
        with unittest.mock.patch.object(
            adapter._client,
            "get_dividends_info",
            return_value=({"AAPL": _AAPL_INFO}, {}),
        ):
            data, _ = adapter.get_dividends_upcoming(["AAPL"])

        self.assertFalse(hasattr(data["AAPL"], "symbol"))
