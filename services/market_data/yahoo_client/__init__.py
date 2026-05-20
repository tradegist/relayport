import logging
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

    def get_dividends_info(self, tickers: list[str]) -> list[DividendInfo]:
        results: list[DividendInfo] = []

        for i, ticker in enumerate(tickers):
            cached = get_cached(ticker, self._cache)
            if cached is not None:
                log.debug("Dividend cache hit for %s", ticker)
                results.append(cached)
                continue

            try:
                if self._session is None:
                    self._session = get_yahoo_session()
                info, updated_session = fetch_with_retry(ticker, self._session)
                self._session = updated_session
                set_cached(ticker, info, self._cache)
                results.append(info)
            except Exception:
                log.debug("Failed to fetch dividend info for %s", ticker, exc_info=True)
                results.append(
                    DividendInfo(
                        ex_div_date=None,
                        payment_date=None,
                        dps=None,
                        are_dates_estimated=False,
                    )
                )

            if i < len(tickers) - 1:
                time.sleep(_INTER_TICKER_DELAY_SECONDS)

        return results

    def clear_cache(self) -> None:
        clear_dividend_info_cache(self._cache)
