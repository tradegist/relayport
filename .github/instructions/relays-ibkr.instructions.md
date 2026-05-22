---
applyTo: "services/relays/ibkr/**"
---

# `services/relays/ibkr/` — IBKR adapter

IBKR demonstrates a complex adapter: XML Flex polling + ibkr_bridge WS listener.

For the fee/timestamp/option conventions that apply across all adapters, see `relays.instructions.md`. For fixture refresh, see the `refresh-flex-fixtures` skill.

## Adapter shape

- **`build_relay(notifiers)`** constructs a `BrokerRelay` with IBKR-specific `PollerConfig`s (Flex fetch + parse callbacks) and an optional `ListenerConfig` (ibkr_bridge WS with bearer token auth).
- **Multi-account support** via `_2` suffixed env vars (e.g. `IBKR_FLEX_QUERY_ID_2`). Each suffix produces an additional `PollerConfig` within the same relay — no separate container. Triggered via `make poll RELAY=ibkr IDX=2` or `POST /relays/ibkr/poll/2`.
- **Relay-specific overrides** — `IBKR_NOTIFIERS`, `IBKR_TARGET_WEBHOOK_URL` override the generic equivalents for the IBKR relay only.
- **Listener connect callback** — closure adds bearer token auth headers and tracks `last_seq` for event resumption across reconnects.

## Flex fetch / dump separation

- **`flex_fetch.py` is a pure library** — exposes `fetch_flex_report()` and `RedactTokenFilter` but contains no CLI code. Imported by `__init__.py` (relay runtime) and `flex_dump.py` (CLI). **Never add `if __name__ == "__main__"` blocks or `argparse` back into `flex_fetch.py`** — causes a `sys.modules` conflict because `__init__.py` imports it at package load time.
- **`flex_dump.py` is the CLI entrypoint** — invoked via `python -m relays.ibkr.flex_dump --token TOKEN --query-id ID [--dump PATH]`. Receives credentials as explicit CLI args (sourced from `.env.relays` by the Makefile) rather than reading env vars directly. Keeps env-var ownership in `__init__.py`'s getters.
- **`RedactTokenFilter` is public** (no underscore) — exported from `flex_fetch.py`, used by both `__init__.py` (relay runtime logging) and `flex_dump.py` (CLI logging). Private (`_`-prefixed) names are only for identifiers with no external consumers.

## Option mapping

For `assetCategory == "OPT"` fills:
- `Fill.symbol = contract.localSymbol.replace(" ", "")` — OCC ticker with spaces stripped (e.g. `"AVGO260620C00200000"`). IBKR pads the underlying to 6 characters with spaces in the raw OCC ticker — always strip so `Fill.symbol` is URL-friendly.
- `Fill.option.rootSymbol = contract.symbol` — underlying (e.g. `"AVGO"`).
- `strike`, `expiryDate` (via `flex_date_to_iso()`), and `type` (`"call"`/`"put"` from the `putCall` attribute) are required. Rows with missing or invalid option metadata are skipped with a parse error.

## Fixture management

- `fixtures/sanitize.py` replaces real account/order/execution IDs in a raw Flex dump with synthetic values, then trims the fixture to at most 6 distinct orders (`max_orders` / `_MAX_ORDERS = 6`), keeping all executions for the retained orders.
- Run `make ibkr-flex-refresh [S=_2]` to fetch a live response, auto-detect the report type (Activity Flex vs Trade Confirmation), sanitize, and write to the appropriate fixture file.
- **Raw dumps must never be committed** — they contain real account IDs. Only `activity_flex_sample.xml` and `trade_confirm_sample.xml` (synthetic IDs only) are committed.
