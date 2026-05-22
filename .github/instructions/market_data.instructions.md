---
applyTo: "services/market_data/**"
---

# `services/market_data/` — Market data service

Standalone aiohttp container serving market data lookups. Has its own `MD_API_TOKEN` (separate from the relay `API_TOKEN`) and its own Docker image. Port 8001.

## Endpoint shape

- **`GET /v1/market-data/dividends/upcoming?symbol=AAPL,MSFT&target=yahoo`** returns `{ data: { [ticker]: DividendsUpcomingItem }, errors: { [ticker]: TickerError } }`. Fetch failures for individual tickers are isolated to `errors` without affecting others. HTTP status is always 200 for valid requests.
- `TickerError` has two fields: `code` (an `ErrorCode` string) and `message` (human-readable detail).
- Two health endpoints: `/health` (unauthenticated, used by Docker `HEALTHCHECK` on the direct container port) and `/v1/market-data/health` (the public path routed through Caddy — same handler, required because Caddy only forwards `/v1/market-data/*`).

## Auth

- **`MD_API_TOKEN`** is required at startup via `validate_api_token()`. Auth middleware rejects empty tokens with HTTP 500 (same pattern as the relay `API_TOKEN` guard).

## Adapter pattern

- `target=yahoo` dispatches to `YahooAdapter` via the registry.
- New providers (e.g. `target=alpha_vantage`) can be added by registering a new `MarketDataAdapter` subclass.
- Adapter instances are **singletons** (cached by class identity in `get_adapter`), so `YahooClient`'s in-memory cache and session are shared across all requests for the lifetime of the process.

## Query-param validation

- `symbol` is parsed by `parse_string_list()` from `utils.py` inside the `DividendsUpcomingQuery.parse_symbol` field validator: uppercases, strips whitespace, deduplicates (order-preserving via `dict.fromkeys`), rejects blank-only input, and enforces `_MAX_SYMBOLS = 20`.
- Validation errors raise `ValueError`, which Pydantic wraps into `ValidationError` caught by the route handler and re-raised as `UserError(VALIDATION_ERROR)`.
- **When adding a new array-of-string query param**, use `parse_string_list(v, max_count=N)` from `market_data.utils` — do not inline.

## Yahoo client (FRAGILE)

- **`auth.py` reverse-engineers Yahoo's session flow.** Uses `curl_cffi` with `impersonate="chrome120"` for browser-matching TLS fingerprints — plain `httpx` is blocked with HTTP 429 by Yahoo's WAF. If requests break, check `yfinance/data.py` first.
- `IMPERSONATE` is a public constant (no leading underscore) because it is imported cross-module by `dividends.py`.
- **Date selection in `fetch_dividend_info_from_yahoo`** — `ex_div_date` and `payment_date` are selected **independently**. For `ex_div_date`: announced if future, else estimated, else `None`. For `payment_date`: announced if future, else estimated (derived from `ex_div_unix_for_payment + payment_offset_seconds`), else `None`. The two do not gate each other. `are_dates_estimated` is `True` when either used the fallback path.
- **In-memory cache + thread safety** — `YahooClient` caches `DividendInfo` per ticker with a TTL (default 12 h). Per-process — no shared state between restarts. All reads/writes/clears guarded by `self._lock` (`threading.Lock`) because `get_dividends_info` runs in a threadpool via `asyncio.to_thread`.
- **Double-check lock pattern in `get_dividend_info`** — snapshot session under lock, network call outside it, re-acquire to re-check cache (another thread may have fetched the same ticker) before writing.
- **Dedicated `_session_init_lock`** serialises `get_yahoo_session()` bootstrap. When `self._session is None`, only one thread enters the init critical section; others wait and skip re-init once it completes.
- `clear_cache` also acquires the lock so `clear_dividend_info_cache`'s dict iteration is protected.
- `get_dividends_info` pre-checks cache under `_lock` before each ticker fetch and only sleeps `_INTER_TICKER_DELAY_SECONDS` on cache misses (not on hits).

## Error handling

Two-class hierarchy in `errors.py`:

- **`AppError(Exception)`** — server-side faults (5xx). Has `code: ErrorCode` and `message`. `str(exc)` renders as `"{message} [{code}]"`. `status_code` property derives HTTP status from `_STATUS_OVERRIDES` (default 500).
- **`UserError(AppError)`** — client-side faults (4xx). Safe to surface. Default 400.
- **`YahooError(AppError)`** — Yahoo-specific. Distinct subclass so `fetch_with_retry` can target it for session refresh on `YAHOO_UNAUTHORIZED`.

`ErrorCode` (`StrEnum`) is the registry. Every `AppError`/`UserError` must use one:

| Code | Class | Default status | Meaning |
|---|---|---|---|
| `YAHOO_UNAUTHORIZED` | `YahooError` | 503 | Session expired and couldn't refresh. Currently always caught per-ticker. |
| `YAHOO_ERROR` | `YahooError` | 500 | Other Yahoo HTTP error. Currently always caught per-ticker. |
| `FETCH_FAILED` | `AppError` | 500 | Unexpected exception during ticker fetch. Currently always caught per-ticker. |
| `INTERNAL_ERROR` | `AppError` | 500 | Server misconfig (e.g. adapter not registered). Surfaces as HTTP 500. |
| `UNAUTHORIZED` | `UserError` | 401 | Missing or invalid `Authorization` header. |
| `VALIDATION_ERROR` | `UserError` | 422 | Query param failed validation. |

The `_STATUS_OVERRIDES` dict in `errors.py` only lists codes whose status differs from the class default. Keep it small.

**Error middleware** is registered **first** in `create_app` so it wraps everything (including auth):

```python
app = web.Application(middlewares=[error_middleware, auth_middleware])
```

Precedence:

1. `web.HTTPException` → `{"error": "{reason} [{status}]"}` JSON. Every non-200 has the same shape; aiohttp default HTML is never returned.
2. `UserError` → `{"error": str(exc)}` with `exc.status_code`; logs at `warning`.
3. `AppError` (non-`UserError`) → `{"error": "Internal server error [INTERNAL_ERROR]"}` with `exc.status_code`; logs full detail at `error`. Detailed message never sent to client.
4. Any other `Exception` → `{"error": "Internal server error [INTERNAL_ERROR]"}` 500; logs at `exception` (full traceback).

**Per-ticker errors** in the batch response are `TickerError` objects with separate `code` and `message` fields — not composite strings. The `YahooAdapter` converts `AppError` instances (returned by `YahooClient.get_dividends_info`) into `TickerError` at the serialisation boundary. The `__str__` composite (`"{message} [{code}]"`) is only used for HTTP-level error responses and logging, never in the structured per-ticker dict.

**Adding new error codes**: add the member to `ErrorCode`, add a row to `_STATUS_OVERRIDES` only if it needs a non-default HTTP status, then `raise AppError(..., ErrorCode.NEW_CODE)` at the call site. Middleware handles the rest.

## TypeScript types

`DividendsUpcomingItem`, `DividendsUpcomingQuery`, `DividendsUpcomingResponse` are generated from `services/market_data/models/dividends.py` via `schema_gen.py` into `types/typescript/market_data_api/`. Exported as the `MarketDataApi` namespace in `types/typescript/index.d.ts`. Run `make types` after any model change.
