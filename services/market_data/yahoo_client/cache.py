import time

from market_data.yahoo_client.types import CacheEntry, DividendInfo

_CACHE_KEY_PREFIX = "dividend_info_"
_CACHE_KEY_VERSION = "v1"
_CACHE_TTL_SECONDS = 12 * 60 * 60

# Type alias for the in-memory cache store owned by the caller.
CacheStore = dict[str, CacheEntry]


def _cache_key(ticker: str) -> str:
    return f"{_CACHE_KEY_PREFIX}{_CACHE_KEY_VERSION}_{ticker}"


def get_cached(ticker: str, cache: CacheStore) -> DividendInfo | None:
    entry = cache.get(_cache_key(ticker))
    if entry is None:
        return None
    if time.time() - entry.cached_at > _CACHE_TTL_SECONDS:
        return None
    return entry.data


def set_cached(ticker: str, data: DividendInfo, cache: CacheStore) -> None:
    cache[_cache_key(ticker)] = CacheEntry(data=data, cached_at=time.time())


def clear_dividend_info_cache(cache: CacheStore) -> None:
    current_prefix = f"{_CACHE_KEY_PREFIX}{_CACHE_KEY_VERSION}_"
    stale_keys = [
        k for k in cache if k.startswith(_CACHE_KEY_PREFIX) and not k.startswith(current_prefix)
    ]
    for k in stale_keys:
        del cache[k]
