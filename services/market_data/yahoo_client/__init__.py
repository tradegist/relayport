import logging
import threading
import time

from market_data.yahoo_client.auth import get_yahoo_session
from market_data.yahoo_client.cache import (
    CacheStore,
    clear_dividend_info_cache,
    get_cached,
    set_cached,
)
from market_data.yahoo_client.dividends import fetch_with_retry
from market_data.yahoo_client.types import DividendInfo, YahooSession

_INTER_TICKER_DELAY_SECONDS = 0.3

log = logging.getLogger(__name__)


class YahooClient:
    def __init__(self) -> None:
        self._session: YahooSession | None = None
        self._cache: CacheStore = {}
        self._lock = threading.Lock()

    def get_dividend_info(self, ticker: str) -> DividendInfo:
        """Return dividend info for one ticker, using the cache.

        Raises on fetch failure — callers that want null-on-failure should
        catch exceptions themselves (or use get_dividends_info).
        """
        with self._lock:
            cached = get_cached(ticker, self._cache)
            if cached is not None:
                log.debug("Dividend cache hit for %s", ticker)
                return cached

            if self._session is None:
                self._session = get_yahoo_session()
            info, self._session = fetch_with_retry(ticker, self._session)
            set_cached(ticker, info, self._cache)
            return info

    def get_dividends_info(
        self, tickers: list[str]
    ) -> tuple[dict[str, DividendInfo], dict[str, str]]:
        """Batch fetch keyed by ticker.

        Successful results go into the first dict; fetch failures go into the
        second dict as error strings. Never raises.
        """
        data: dict[str, DividendInfo] = {}
        errors: dict[str, str] = {}

        for i, ticker in enumerate(tickers):
            try:
                data[ticker] = self.get_dividend_info(ticker)
            except Exception as exc:
                log.debug("Failed to fetch dividend info for %s", ticker, exc_info=True)
                errors[ticker] = str(exc)

            if i < len(tickers) - 1:
                time.sleep(_INTER_TICKER_DELAY_SECONDS)

        return data, errors

    def clear_cache(self) -> None:
        clear_dividend_info_cache(self._cache)
