# `services/relays/kraken/` — Kraken adapter

Kraken demonstrates a simpler adapter: JSON REST polling (TradesHistory) + native WS v2 listener (executions channel).

For the fee/timestamp/option conventions, see [services/relays/CLAUDE.md](../CLAUDE.md).

## Single shared `KrakenClient` (NONCE CORRECTNESS)

- **`build_relay()` calls `_resolve_client()` once and passes the resulting client to both `_build_poller_configs()` and `_build_listener_config()`.**
- This is **required** for nonce correctness: Kraken tracks the highest nonce ever seen per API key and rejects any request with a lower nonce (`EAPI:Invalid nonce`).
- The `KrakenClient` holds a `threading.Lock` and a `_last_nonce` floor so nonces are strictly monotonic even when poller and listener fire concurrently.
- **Never create separate `KrakenClient` instances for the poller and listener** — they would race on nonce ordering and Kraken would reject requests.

## Adapter shape

- **Poller** — `KrakenClient.get_trades_history()` returns JSON. The parse callback maps each trade via `_parse_rest_trade()` into a `Fill` with `source="rest_poll"`.
- **API errors carry codes** — `KrakenClient._request` raises `KrakenAPIError` (a `RuntimeError` subclass) with Kraken's `error` array in `.errors`; classify with `has_any(codes)`, which also matches codes followed by `:detail`. Never match on message text.
  - **Poller** — codes in `_NEEDS_ACTION_ERRORS` raise `FetchConfigError` (immediate alert, 2 h retry pause): the credential/account set `_CREDENTIAL_ERRORS` (`EAPI:Invalid key`, `EAPI:Invalid signature`, `EGeneral:Permission denied`, `EAccount:Invalid permissions`, `EAuth:Account temporary disabled`, `EAuth:Account unconfirmed`) plus `EGeneral:Temporary lockout`, `EGeneral:Invalid arguments`, `ETrade:Invalid request`. Everything else (`EService:*`, `EGeneral:Internal error`, rate limits, `EAPI:Invalid nonce`, HTTP/network errors) is a plain `FetchError`.
  - **Listener** — a `_CREDENTIAL_ERRORS` code from `GetWebSocketsToken` raises `FatalListenerError` (stop + immediate alert); other errors propagate so the engine reconnects.
- **Listener** — the `connect` callback obtains a short-lived WS token via REST (`GetWebSocketsToken`), opens a websocket to `wss://ws-auth.kraken.com/v2`, sends a subscription for the `executions` channel, and returns the ready websocket. The `on_message` callback uses `ws_parser.parse_executions()` to extract multiple fills per message with `source="ws_execution"`.

## Asset class

- **All asset classes are `"crypto"`.** Kraken is a crypto-only exchange — no conditional logic needed.

## Fee + dedup notes (Kraken-specific)

- Kraken emits **different identifiers on REST vs WS**: WS sends per-match `exec_id`s; REST returns a single consolidated `txid` matching none of them. The order-level dedup layer (listener writes `orderId` + `execId` together; poller drops candidates whose `orderId` was processed by the listener within `2 × POLL_INTERVAL`) is what catches this case.
- Side effect: when the listener wins, the poller's fee-bearing webhook for that order is suppressed. If real-time fee accuracy matters, consider running Kraken in poller-only mode with a shorter `KRAKEN_POLL_INTERVAL`.
