# `services/relays/ibkr/` — IBKR adapter

IBKR demonstrates a complex adapter: XML Flex polling + ibkr_bridge WS listener.

For the fee/timestamp/option conventions that apply across all adapters, see [services/relays/CLAUDE.md](../CLAUDE.md). For fixture refresh, see the [`refresh-flex-fixtures`](../../../.claude/skills/refresh-flex-fixtures/SKILL.md) skill.

## Adapter shape

- **`build_relay(notifiers)`** constructs a `BrokerRelay` with IBKR-specific `PollerConfig`s (Flex fetch + parse callbacks) and an optional `ListenerConfig` (ibkr_bridge WS with bearer token auth).
- **Multi-account support** via `_2` suffixed env vars (e.g. `IBKR_FLEX_QUERY_ID_2`). Each suffix produces an additional `PollerConfig` within the same relay — no separate container. Triggered via `make poll RELAY=ibkr IDX=2` or `POST /relays/ibkr/poll/2`.
- **Relay-specific overrides** — `IBKR_NOTIFIERS`, `IBKR_TARGET_WEBHOOK_URL` override the generic equivalents for the IBKR relay only.
- **Listener connect callback** — closure adds bearer token auth headers and tracks `last_seq` for event resumption across reconnects. The seq is persisted to `/data/meta/relay.db` (via `get_last_bridge_seq` / `set_last_bridge_seq` in `poller_engine.py`) on every received message and read back on startup, so a relay restart resumes from the last delivered event instead of replaying the bridge's full buffer from seq=0 (which would cause duplicate webhooks).

## Flex fetch / dump separation

- **`flex_fetch.py` is a pure library** — exposes `fetch_flex_report()` and `RedactTokenFilter` but contains no CLI code. Imported by `__init__.py` (relay runtime) and `flex_dump.py` (CLI). **Never add `if __name__ == "__main__"` blocks or `argparse` back into `flex_fetch.py`** — causes a `sys.modules` conflict because `__init__.py` imports it at package load time.
- **`flex_dump.py` is the CLI entrypoint** — invoked via `python -m relays.ibkr.flex_dump --token TOKEN --query-id ID [--dump PATH]`. Receives credentials as explicit CLI args (sourced from `.env.relays` by the Makefile) rather than reading env vars directly. Keeps env-var ownership in `__init__.py`'s getters.
- **`RedactTokenFilter` is public** (no underscore) — exported from `flex_fetch.py`, used by both `__init__.py` (relay runtime logging) and `flex_dump.py` (CLI logging). Private (`_`-prefixed) names are only for identifiers with no external consumers.

## Option mapping

For `assetCategory == "OPT"` fills:
- `Fill.symbol = contract.localSymbol.replace(" ", "")` — OCC ticker with spaces stripped (e.g. `"AVGO260620C00200000"`). IBKR pads the underlying to 6 characters with spaces in the raw OCC ticker — always strip so `Fill.symbol` is URL-friendly.
- `Fill.option.rootSymbol = contract.symbol` — underlying (e.g. `"AVGO"`).
- `strike`, `expiryDate` (via `flex_date_to_iso()`), and `type` (`"call"`/`"put"` from the `putCall` attribute) are required. Rows with missing or invalid option metadata are skipped with a parse error.
- **`Fill.cost` magnitude = `price × volume × contract.multiplier`** (bridge path) — options are priced per share; each contract covers `multiplier` shares (standard = 100). `_map_fill` parses `WsContract.multiplier` as `int`; a non-integer or non-positive value raises `ValueError` and is surfaced as a webhook parse error. Equity fills use `multiplier = 1` so the formula is uniform. This computes the **magnitude only** — the sign is applied afterwards by `Fill` (negative on buy, positive on sell; see [services/relays/CLAUDE.md](../CLAUDE.md)), so `_map_fill` must not sign it. The multiplier does not apply to the Flex path (IBKR pre-calculates `cost` in the XML), but Flex is subject to the same re-signing: it reports `cost` as an inverted cost-basis delta.

## Book-trade classification (`_book_trade_key`)

Implements the cross-path dedup contract from [services/relays/CLAUDE.md](../CLAUDE.md):

- **Flex path**: `raw["transactionType"] == "BookTrade"` (primary — verified on live assignment rows; normal fills carry `ExchTrade`), falling back to exact-token intersection of `raw["notes"]` (Activity Flex) / `raw["code"]` (Trade Confirmation) with `{A, Ex, Ep, AEx, MEx, GEA}`. Tokens are split on `;` and matched exactly — substring matching would confuse `A` with `Adj`/`Al`/`Aw` and `Ex` with `AEx`. Both columns are user-selected in the Flex query config; a query without them fails open to duplicate webhooks (documented in README).
- **Bridge path**: the `isBookTrade` envelope field, set by ibkr_bridge for reconciled executions that never received a CommissionReport. Do NOT classify on `lastLiquidity == 2147483647` — that is TWS `UNSET_INTEGER` ("no data"), not an assignment marker.
- Account comes from `raw["accountId"]` (Flex) / `raw["fill"]["execution"]["acctNumber"]` (bridge envelope dump); missing account → `None` (fail open, never an account-blind key).

## Combo (multi-leg / BAG) orders

- **`_map_fill` skips `secType == "BAG"` executions** (bridge path). IBKR emits a synthetic "combo summary" execution for multi-leg orders — reported under the *underlying* symbol, with zero commission and the parent order's `permId`. It is not a tradeable leg (the real legs arrive as their own executions), and it shares the legs' `permId` (→ `Fill.orderId`). Left in, it would (1) fire a phantom underlying-symbol webhook and (2) merge with the legs in `aggregate_fills`. The Flex path never sees it (IBKR omits the combo leg from Flex reports).
- **Combo-leg execIds differ between TWS and Flex** — TWS appends a 5th dot-segment to combo-leg execIds (`0000fb0a.xxxxxxxx.02.01.01`); Flex reports the same execution truncated to its 4-segment prefix (`0000fb0a.xxxxxxxx.02.01`). Without reconciliation each path treats the other's ID as unseen and re-delivers the fill (confirmed for every production combo order). `_dedup_aliases` maps the 5-segment form to its 4-segment prefix and is registered as `BrokerRelay.dedup_aliases`; the listener engine marks and checks the alias alongside the real ID. Flex fallback IDs (numeric `transactionId` when `ibExecID` is empty) have no alias — those are book trades (assignments/exercises/expiries), reconciled by the book-trade economic-key layer (`_book_trade_key`, above).
- **`aggregate_fills` groups by `(orderId, symbol)`, not `orderId` alone** — the second reason combo legs stay separate: even sharing one `permId`, distinct option contracts have distinct `Fill.symbol`, so each leg becomes its own `Trade`.

## Fixture management

- `fixtures/sanitize.py` replaces real account/order/execution IDs in a raw Flex dump with synthetic values, then trims the fixture to at most 6 distinct orders (`max_orders` / `_MAX_ORDERS = 6`), keeping all executions for the retained orders.
- Run `make ibkr-flex-refresh [S=_2]` to fetch a live response, auto-detect the report type (Activity Flex vs Trade Confirmation), sanitize, and write to the appropriate fixture file.
- **Raw dumps must never be committed** — they contain real account IDs. Only `activity_flex_sample.xml` and `trade_confirm_sample.xml` (synthetic IDs only) are committed.
