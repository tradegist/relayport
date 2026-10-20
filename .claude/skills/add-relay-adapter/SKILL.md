---
name: add-relay-adapter
description: Step-by-step procedure to add a new broker relay adapter (after IBKR/Kraken). Use when the user asks to "add a new broker", "support relay X", "add Kraken/Binance/etc", or otherwise indicates a new broker integration. Covers the full 11-step contract from shared types through tests and README.
---

# Adding a new relay adapter — step-by-step

Use the existing `ibkr` and `kraken` relays as reference implementations. IBKR demonstrates a complex adapter (XML polling + bridge WS with two event types); Kraken demonstrates a simpler one (JSON REST polling + native WS with token-based auth).

## 1. Update shared types (`services/shared/models.py`)

- Add the relay name to `RelayName` (e.g. `Literal["ibkr", "kraken", "newbroker"]`).
- Add any new source identifiers to `Source` (e.g. `"newbroker_rest"`, `"newbroker_ws"`).

## 2. Create the relay adapter package (`services/relays/<name>/`)

- `__init__.py` — must export `build_relay(notifiers: list[BaseNotifier]) -> BrokerRelay`. This is the only contract the registry requires.
- Add broker-specific TypedDicts for raw API shapes (e.g. `<name>_types.py`).
- Add a REST client if the broker has a REST API (e.g. `rest_client.py`).
- Add a WS parser if the broker has a WebSocket API (e.g. `ws_parser.py`).

## 3. Implement `build_relay()` — must return a `BrokerRelay` with:

- `name`: the relay name (must match `RelayName`).
- `notifiers`: pass through from the argument.
- `poller_configs`: list of `PollerConfig` (can be empty if listener-only). Each needs:
  - `fetch: Callable[[], str]` — returns raw data (JSON string, XML, etc.); raises `relay_core.FetchError` on failure. The message reaches logs and the operator's outage alert verbatim, so name the failing step and the broker's error (`"Flex SendRequest failed: [1012] Token has expired"`) and never include credentials — redact any URL that carries them. Wrap only expected failures (HTTP, API errors, bad response shape); let programming errors propagate with their traceback.
  - `parse: Callable[[str], tuple[list[Fill], list[str]]]` — parses raw data into (fills, errors).
  - `interval: int` — poll interval in seconds.
- `listener_config`: a `ListenerConfig` or `None` (can be None if poller-only). Needs:
  - `connect: Callable[[aiohttp.ClientSession], Awaitable[aiohttp.ClientWebSocketResponse]]` — async callback that connects, authenticates, subscribes, and returns a ready-to-read websocket. The engine handles reconnection with exponential backoff; this callback is called on each reconnect.
  - `on_message: Callable[[dict], Awaitable[list[OnMessageResult]]]` — parses a WS JSON dict into a list of `OnMessageResult`. Each has four fields:
    - `fill: Fill | None` — parsed fill, or `None` to skip.
    - `mark: bool` — `True` routes through dedup+notify+mark; `False` is fire-and-forget (no dedup, no mark) for preliminary events.
    - `error: str | None` — human-readable reason a fill was dropped (`fill=None`). Surfaced in the webhook payload's `errors`.
    - `order_complete: bool` — `True` on the fill event that closes its order. Debounce buffer flushes that orderId immediately. Leave `False` if the broker exposes no per-order lifecycle signal.
  - `event_filter: Callable[[dict], bool]` — return True for events that should reach `on_message`, False to skip (heartbeats, subscription acks).
  - `debounce_ms: int` — optional debounce buffer (0 = disabled). Per-orderId; one order's fills never delay another order's flush.
  - `health_check: Callable[[aiohttp.ClientSession], Awaitable[str | None]] | None` — optional. Needed only when the WS source relays another connection it can lose while the socket stays open (ibkr_bridge → IB Gateway). Return `None` when healthy, else a credential-free reason. The engine already tracks its own WS state; outage alerts for pollers and listeners need no adapter code.

## 4. Environment variables — follow the prefix convention

- Use `{RELAY}_` prefix for all relay-specific vars (e.g. `KRAKEN_API_KEY`).
- Use `relay_core.env.get_env()` / `get_env_int()` for vars that support prefix fallback.
- Use direct `os.environ.get()` wrapped in getter functions for broker-specific vars with no generic equivalent.
- Add the vars to `env_examples/env.relays` — **mandatory**. Follow the existing relay sections as a model.
- Update the `RELAYS` comment in `env_examples/env` to include the new relay name.

## 5. Register the module

- `pyproject.toml`: add to `tool.pytest.ini_options.testpaths`, `tool.ruff.src`, `tool.ruff.lint.isort.known-first-party`.
- Makefile: add to `lint:` and `typecheck:` targets.
- `.dockerignore`: add `!services/relays/<name>/**` if needed (currently `!services/relays/**` covers all relay packages).

## 6. Timestamp normalisation

Every `Fill.timestamp` must be `YYYY-MM-DDTHH:MM:SS` (UTC, no `Z`, no fractional seconds). If the broker's native timestamp format is not ISO-8601, add `services/relays/<name>/timestamps.py` with a small `<format>_to_iso(raw) -> str` helper using `strptime` to validate strictly. The parser chains it as `normalize_timestamp(<format>_to_iso(raw), assume_tz=tz)`. **Never add broker format knowledge to `services/shared/time_format.py`** — keep it broker-agnostic.

## 7. Option contracts

If the broker supports options, populate `Fill.option` (`OptionContract`) when `assetClass == "option"`:
- `rootSymbol: str` — underlying ticker
- `strike: float`
- `expiryDate: str` — ISO `YYYY-MM-DD`. Use `flex_date_to_iso()` (or broker equivalent)
- `type: Literal["call", "put"]`

**Never emit a fill with `assetClass == "option"` when option metadata is missing or invalid** — skip the row and surface a parse error instead.

## 8. Write tests

Colocate unit tests next to source: `test_<name>.py`. If you added `timestamps.py`, add `test_timestamps.py` with positive + negative cases — the helper's job is to reject typos that `datetime.fromisoformat` would silently accept.

Cover the fetch failure contract: each expected failure (HTTP error, broker API error, malformed response) raises `FetchError` whose message names the broker's error, and an unexpected exception propagates with its own type. If the fetch URL carries a credential, assert it is absent from the message. If you added a `health_check`, test each outcome against a real local server (`aiohttp.test_utils.TestServer`): healthy → `None`; broker-side down, HTTP error, and malformed body → a reason. See `TestBridgeHealthCheck` in `relays/ibkr/test_ibkr.py`.

## 9. Understand the listener/poller dedup interaction

When both `LISTENER_ENABLED` and `POLLER_ENABLED` are true for the same relay, two reconciliation layers run:

- **exec_id dedup** (always on): listener writes `execId` to the shared dedup DB; poller skips known `execId`s. Works when broker uses the **same identifier** on WS and REST (IBKR).
- **order-level dedup** (always on, listener-side write): listener stores `orderId` + `execId`. Poller drops candidates whose `orderId` was processed by the listener within `2 × POLL_INTERVAL`. Catches brokers where REST and WS identifiers differ (Kraken multi-match).

Verify experimentally whether the broker reuses identifiers across paths. If it does not, the order-level dedup will suppress the poller's fee-bearing webhook — document the fee trade-off.

## 10. Update README

Add the relay's env vars, webhook payload examples, broker-specific setup. If the listener does not reliably include fees, document the listener vs poller trade-off explicitly (see the Kraken section as a template).

## 11. Verify

`make test`, `make typecheck`, `make lint` must all pass before deploying.

## Cross-cutting conventions to read first

- **Fee normalisation** — see `services/relays/CLAUDE.md`: prefer `*_usd_equiv`, single-asset only, `abs()` per entry.
- **Timestamp normalisation** — same file: two-layer split (relay-owned `<format>_to_iso` + shared `normalize_timestamp`).
