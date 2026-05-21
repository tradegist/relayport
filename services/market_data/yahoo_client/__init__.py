import logging
import threading
import time

from market_data.errors import AppError, ErrorCode, YahooError
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
        self._session_init_lock = threading.Lock()

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
            session = self._session

        if session is None:
            # Serialise session bootstrap so only one thread calls get_yahoo_session().
            # Threads that lose the race re-check self._session under _lock and reuse
            # the result — preventing redundant session requests that risk rate limiting.
            with self._session_init_lock:
                with self._lock:
                    session = self._session
                if session is None:
                    session = get_yahoo_session()
                    with self._lock:
                        self._session = session

        info, session = fetch_with_retry(ticker, session)

        with self._lock:
            # Double-check: another thread may have fetched the same ticker
            # while we were doing the network call.
            cached = get_cached(ticker, self._cache)
            if cached is not None:
                log.debug("Dividend cache hit for %s (race)", ticker)
                return cached
            self._session = session
            set_cached(ticker, info, self._cache)
        return info

    def get_dividends_info(
        self, tickers: list[str]
    ) -> tuple[dict[str, DividendInfo], dict[str, AppError]]:
        """Batch fetch keyed by ticker.

        Successful results go into the first dict; fetch failures go into the
        second dict as AppError instances. Never raises.
        """
        data: dict[str, DividendInfo] = {}
        errors: dict[str, AppError] = {}

        for i, ticker in enumerate(tickers):
            try:
                data[ticker] = self.get_dividend_info(ticker)
            except YahooError as exc:
                errors[ticker] = exc
                log.warning("Yahoo Finance error for %s: %s", ticker, exc)
            except Exception:
                errors[ticker] = AppError(
                    f"Unexpected error fetching dividend info for {ticker}",
                    ErrorCode.FETCH_FAILED,
                )
                log.exception("Unexpected error fetching dividend info for %s", ticker)

            if i < len(tickers) - 1:
                time.sleep(_INTER_TICKER_DELAY_SECONDS)

        return data, errors

    def clear_cache(self) -> None:
        with self._lock:
            clear_dividend_info_cache(self._cache)
