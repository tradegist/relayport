import unittest
import unittest.mock

from market_data.yahoo_client.cache import (
    _CACHE_KEY_VERSION,
    _CACHE_TTL_SECONDS,
    CacheStore,
    _cache_key,
    clear_dividend_info_cache,
    get_cached,
    set_cached,
)
from market_data.yahoo_client.types import CacheEntry, DividendInfo

_FROZEN_NOW = 1768435200.0  # 2026-01-15T00:00:00Z

_SAMPLE = DividendInfo(
    ex_div_date="2026-02-15",
    payment_date="2026-03-08",
    dps=1.5,
    annual_dps=1.5,
    are_dates_estimated=False,
)


class TestGetCached(unittest.TestCase):
    def test_returns_none_when_no_entry(self) -> None:
        cache: CacheStore = {}
        self.assertIsNone(get_cached("AAPL", cache))

    def test_returns_data_within_ttl(self) -> None:
        cache: CacheStore = {
            _cache_key("AAPL"): CacheEntry(data=_SAMPLE, cached_at=_FROZEN_NOW - 1)
        }
        with unittest.mock.patch("time.time", return_value=_FROZEN_NOW):
            result = get_cached("AAPL", cache)
        self.assertEqual(result, _SAMPLE)

    def test_returns_none_past_ttl(self) -> None:
        cache: CacheStore = {
            _cache_key("AAPL"): CacheEntry(data=_SAMPLE, cached_at=_FROZEN_NOW - _CACHE_TTL_SECONDS - 3600)
        }
        with unittest.mock.patch("time.time", return_value=_FROZEN_NOW):
            result = get_cached("AAPL", cache)
        self.assertIsNone(result)


class TestSetCached(unittest.TestCase):
    def test_stores_entry_with_current_timestamp(self) -> None:
        cache: CacheStore = {}
        with unittest.mock.patch("time.time", return_value=_FROZEN_NOW):
            set_cached("AAPL", _SAMPLE, cache)

        key = f"dividend_info_{_CACHE_KEY_VERSION}_AAPL"
        self.assertIn(key, cache)
        entry = cache[key]
        self.assertEqual(entry.data, _SAMPLE)
        self.assertEqual(entry.cached_at, _FROZEN_NOW)


class TestClearDividendInfoCache(unittest.TestCase):
    def test_deletes_stale_version_keys_while_keeping_current(self) -> None:
        entry = CacheEntry(data=_SAMPLE, cached_at=_FROZEN_NOW)
        cache: CacheStore = {
            "dividend_info_v0_AAPL": entry,   # stale version → delete
            "dividend_info_v1_AAPL": entry,   # current version → keep
            "dividend_info_v1_MSFT": entry,   # current version → keep
        }
        clear_dividend_info_cache(cache)

        self.assertNotIn("dividend_info_v0_AAPL", cache)
        self.assertIn("dividend_info_v1_AAPL", cache)
        self.assertIn("dividend_info_v1_MSFT", cache)

    def test_does_not_touch_unrelated_keys(self) -> None:
        entry = CacheEntry(data=_SAMPLE, cached_at=_FROZEN_NOW)
        cache: CacheStore = {
            "unrelated_key": entry,
            "dividend_info_v1_AAPL": entry,
        }
        clear_dividend_info_cache(cache)
        self.assertIn("unrelated_key", cache)
        self.assertIn("dividend_info_v1_AAPL", cache)
