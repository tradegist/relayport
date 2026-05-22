# `services/shared/` — Shared models + utilities

Library package (no container). Imported by every other service.

For the three-location model layout and the rule about model shims, see [services/CLAUDE.md](../CLAUDE.md). For timestamp/fee conventions, see [services/relays/CLAUDE.md](../relays/CLAUDE.md).

## Module responsibilities

- **`models.py`** — CommonFill primitives: `Fill`, `Trade`, `OptionContract`, `BuySell`, `AssetClass`, `OrderType`, `Source`, `RelayName`. Pydantic models with `ConfigDict(extra="forbid")` on external-contract types. The `__init__.py` barrel re-exports these.
- **`utilities.py`** — Internal helpers: `aggregate_fills`, `normalize_order_type`, `normalize_asset_class`, `_dedup_id`. Not re-exported by model shims (consumers import directly: `from shared import aggregate_fills`).
- **`time_format.py`** — `normalize_timestamp(iso, *, assume_tz=None)`. **Broker-agnostic.** Only accepts ISO-8601. Never teach it about broker-specific formats — those belong in `services/relays/<name>/timestamps.py`.

## Rules

- **`shared/models.py` is the source of truth for the CommonFill contract.** When changing a primitive (adding a field, narrowing a type, renaming an enum value), run `make types` to regenerate the TypeScript + Python type packages and propagate the change to consumers.
- **`models.py` is listed in `schema_gen.py:SCHEMA_MODELS` under key `"shared"`.**
- **Never add broker format knowledge to `time_format.py`.** Each broker's native format → ISO-8601 conversion lives in `services/relays/<name>/timestamps.py`. `normalize_timestamp` is the second layer that converts ISO → canonical UTC.
