# Migration: split `Fill` / `Trade` into discriminated unions by `assetClass`

Status: **not started**. The current production shape is a single-class `Fill` / `Trade` with an optional `option: OptionContract | None` field that is populated only when `assetClass == "option"`. This document is a self-contained handoff for an agent or engineer who later decides to evolve those models into per-asset-class variants.

## When to do this migration

Run this migration when **at least one** of these is true:

- A second asset class needs its own type-specific fields (e.g. equity wants `dividend`, crypto wants `networkFee`/`txHash`, future wants its own contract metadata).
- Generated TypeScript consumers are accumulating noise from `null`-checking fields that don't apply to their `assetClass` (e.g. always doing `trade.option!.strike` with non-null assertions because they "know" it's an option).
- A type-system contract is desired where invalid combinations cannot be constructed — e.g. an equity fill with `option` populated must fail at validation, not just silently round-trip.

If only the option variant ever grows new fields, **do not migrate**. The single-class `option: OptionContract | None` shape will keep working cleanly. Discriminated unions pay off when *multiple* asset classes carry per-class data.

## Starting point (assumed current state)

`services/shared/models.py` looks like this:

```python
AssetClass = Literal["equity", "option", "crypto", "future", "forex", "other"]


class OptionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra=_all_fields_required)
    rootSymbol: str
    strike: float
    expiryDate: str       # YYYY-MM-DD
    type: Literal["call", "put"]


class Fill(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra=_all_fields_required)
    execId: str
    orderId: str
    symbol: str
    assetClass: AssetClass
    side: BuySell
    orderType: OrderType | None = None
    price: float
    volume: float
    cost: float
    fee: float
    timestamp: str
    source: Source
    currency: str | None = None
    option: OptionContract | None = None
    raw: dict[str, Any]


class Trade(BaseModel):
    # …same shape as Fill plus aggregation-only fields (fillCount, execIds, fxRate*)
    option: OptionContract | None = None
```

`aggregate_fills` in `services/shared/utilities.py` constructs a single flat `Trade(...)` and copies `option` from the last fill, mirroring how `currency` and `rootSymbol` were handled before this refactor.

## Target state

```python
class _BaseFill(BaseModel):
    """Common fields shared by every fill variant.

    Private — public consumers always work with the discriminated `Fill`
    union below. The class is exported from the module solely so variant
    classes can inherit; do not construct it directly.
    """
    model_config = ConfigDict(extra="forbid", json_schema_extra=_all_fields_required)
    execId: str
    orderId: str
    symbol: str
    side: BuySell
    orderType: OrderType | None = None
    price: float
    volume: float
    cost: float
    fee: float
    timestamp: str
    source: Source
    currency: str | None = None
    raw: dict[str, Any]


class OptionFill(_BaseFill):
    assetClass: Literal["option"] = "option"
    option: OptionContract


class EquityFill(_BaseFill):
    assetClass: Literal["equity"] = "equity"


class CryptoFill(_BaseFill):
    assetClass: Literal["crypto"] = "crypto"


class FutureFill(_BaseFill):
    assetClass: Literal["future"] = "future"


class ForexFill(_BaseFill):
    assetClass: Literal["forex"] = "forex"


class OtherFill(_BaseFill):
    assetClass: Literal["other"] = "other"


Fill = Annotated[
    OptionFill | EquityFill | CryptoFill | FutureFill | ForexFill | OtherFill,
    Field(discriminator="assetClass"),
]
```

`Trade` mirrors this exactly: same six variants (`OptionTrade`, `EquityTrade`, …), each inheriting from a `_BaseTrade` that adds `fillCount`, `execIds`, `fxRate`, `fxRateBase`, `fxRateSource` to the `_BaseFill` shape.

## Wire-format impact

The serialized JSON shape **does not change** — `assetClass` was already the discriminator. What changes is the *schema*:

- Today's JSON Schema describes a single object where `option` is nullable.
- After migration, the schema is a `oneOf` keyed by `assetClass`, with `option` *required* on the option variant and *forbidden* on every other variant.

Generated TypeScript consumers move from a single `Trade` interface to `Trade = OptionTrade | EquityTrade | …`. Code using `trade.assetClass === "option"` gets free narrowing. Code that was using `trade.option!.strike` (non-null assertion) must switch to a discriminator check or `isinstance` (Python).

External webhook consumers reading our JSON see no payload-level change.

## Migration steps

### 1. Models — `services/shared/models.py`

Add `_BaseFill` and `_BaseTrade` private classes carrying the common fields. Define one variant class per `AssetClass` literal for both Fill and Trade. Replace the public `Fill` / `Trade` symbols with `Annotated[..., Field(discriminator="assetClass")]` aliases.

Re-export the variant classes from `services/shared/__init__.py` so consumers can `from shared import OptionFill, OptionTrade` for `isinstance` narrowing.

Keep `OptionContract` exactly as-is — it does not need changes.

### 2. Aggregation — `services/shared/utilities.py`

`aggregate_fills` currently builds a flat `Trade(...)`. Replace with a small dispatch on the last fill's variant:

```python
last = max(order_fills, key=lambda f: f.timestamp)
common = dict(
    orderId=last.orderId,
    symbol=last.symbol,
    side=last.side,
    orderType=last.orderType,
    price=round(avg_price, 8),
    volume=total_volume,
    cost=round(total_cost, 4),
    fee=round(total_fee, 4),
    fillCount=len(order_fills),
    execIds=[f.execId for f in order_fills],
    timestamp=last.timestamp,
    source=last.source,
    currency=last.currency,
    raw=order_fills[0].raw,
)
match last:
    case OptionFill():
        trades.append(OptionTrade(**common, option=last.option))
    case EquityFill():
        trades.append(EquityTrade(**common))
    case CryptoFill():
        trades.append(CryptoTrade(**common))
    case FutureFill():
        trades.append(FutureTrade(**common))
    case ForexFill():
        trades.append(ForexTrade(**common))
    case OtherFill():
        trades.append(OtherTrade(**common))
```

A helper that builds the `common` dict once and dispatches via `match` is cleaner than six near-duplicate constructor calls.

### 3. Broker parsers

Every adapter currently constructs a flat `Fill(...)`. After the refactor each constructs the variant matching the normalized asset class.

- **`services/relays/ibkr/flex_parser.py::parse_fills`** — switch on the normalized `asset_class` value to pick `OptionFill` (with `option=OptionContract(...)` built from `underlyingSymbol` / `strike` / `expiry` / `putCall`) vs the appropriate non-option variant. Today this file populates `Fill.option` on the option branch only; the dispatch logic is essentially the same shape.
- **`services/relays/ibkr/__init__.py::_map_fill`** — same pattern on the WS path. `OptionFill` is built using `contract.localSymbol` for `symbol` and `OptionContract(rootSymbol=contract.symbol, strike=contract.strike, expiryDate=…, type=…)`. Non-option fills become `EquityFill` etc.
- **`services/relays/kraken/ws_parser.py`** and **`services/relays/kraken/__init__.py`** — Kraken is crypto-only today, so every constructed fill becomes a `CryptoFill`. Trivial.

If a future relay adds new asset classes (e.g. futures), the parsers map to the right variant naturally.

### 4. Notifier payload — `services/relay_core/notifier/models.py`

`WebhookPayloadTrades.data` is typed as `list[Trade]`. With `Trade` now an `Annotated[..., Field(discriminator=…)]` alias, Pydantic handles validation and serialization automatically — **no code change in the payload model**. Verify after step 5 that the regenerated JSON Schema produces the expected `oneOf` shape on `data[*]`.

### 5. Schema and type regeneration

Run `make types`. Then verify by inspection:

- `types/typescript/shared/types.d.ts` and `types/typescript/relay_api/types.d.ts` declare the union with `assetClass` as the discriminator (look for `oneOf` in the intermediate `*.schema.json` and a top-level `Fill = OptionFill | EquityFill | …` in the generated `.d.ts`).
- `types/python/relayport_types/shared.py` re-exports both the union alias and every variant class.
- `types/python/relayport_types/__init__.py` (or equivalent barrel) exposes the new variants.

Commit the regenerated diff in the same PR.

### 6. Tests

Most existing tests assert on shared fields (`fill.symbol`, `fill.side`, `trade.fee`) that remain on the base — these pass without change. Tests that touch option-specific data need `isinstance` narrowing:

```python
fill = parse_fills(xml)[0]
assert isinstance(fill, OptionFill)
assert fill.option.rootSymbol == "AVGO"
```

Add new tests for the discrimination contract in `services/shared/test_*.py` (or alongside `models.py`):

- An option payload **without** an `option` field fails Pydantic validation.
- An equity payload **with** an `option` field fails Pydantic validation (because `EquityFill` has `extra="forbid"` and doesn't declare `option`).
- Round-trip JSON serialize → `model_validate_json` → equality, for at least one variant per asset class.
- Mixed-list validation: `TypeAdapter(list[Fill]).validate_python([…])` accepts a list with multiple variants and rejects payloads whose `assetClass` value isn't in the literal.

### 7. Validation gates

Before merging:

```bash
make typecheck   # mypy strict — must pass
make test        # all unit + new variant tests — must pass
make lint        # ruff
make types       # regenerate; commit the diff
make e2e         # smoke test the wire format end-to-end
```

## Estimated effort

Roughly **3–4 hours of focused work** plus review:

- Models refactor: ~1h
- Parsers + `aggregate_fills` dispatch: ~1h
- Test updates and new discrimination tests: ~1h
- Type regeneration and verification: ~30min

## Risks and gotchas

- **Empty variants are awkward but correct.** `EquityFill`, `CryptoFill`, `ForexFill`, `OtherFill` (and their `Trade` siblings) carry no per-class fields today. They're three-line classes that exist solely so the union is exhaustive. Keep them — deleting them collapses the discriminator's value. Document this in a one-line module comment.
- **mypy narrowing on `Literal` discriminators is limited.** `isinstance(fill, OptionFill)` narrows correctly in mypy. `if fill.assetClass == "option": …` does **not** narrow in mypy (this is a known mypy limitation with Pydantic discriminated unions). Prefer `isinstance` in Python code. The TypeScript consumer side narrows via the discriminator string fine.
- **`json_schema_extra=_all_fields_required` propagation.** Verify the hook is applied via `_BaseFill.model_config` and that variant classes inherit it correctly. Without it, generated JSON Schema marks fields as optional and TypeScript types become `field?: T | null`.
- **Discriminator default values.** Each variant declares `assetClass: Literal["option"] = "option"` (with a default). The default lets internal code construct without repeating the discriminator (`OptionFill(execId=…, …)`); the `Literal` keeps Pydantic and JSON Schema strict.
- **Breaking change for internal callers.** Code that currently does `Fill(assetClass="option", …)` will not type-check after the refactor — callers must use `OptionFill(…)`. There's no shim. The wire format is unchanged for external consumers, so the break is internal-only.
- **`raw: dict[str, Any]`** stays on `_BaseFill` and is not part of the discriminated contract — it remains a free-form bag for broker-specific debugging data.

## Out of scope

- Changing the wire JSON format. The discriminator was already on the wire.
- Adding new asset classes (e.g. `"warrant"`, `"cfd"`). Add them in a separate change once the framework is in place.
- Reshaping `OptionContract` itself. Migrate the union first, iterate on `OptionContract` independently if its fields need to grow.

## Reference: file inventory

Files this migration touches, in order of impact:

- `services/shared/models.py` — split into base + variants (largest change)
- `services/shared/__init__.py` — re-export variants
- `services/shared/utilities.py` — `aggregate_fills` dispatch
- `services/relays/ibkr/flex_parser.py` — pick variant per row
- `services/relays/ibkr/__init__.py` — `_map_fill` picks variant
- `services/relays/kraken/ws_parser.py` — always `CryptoFill`
- `services/relays/kraken/__init__.py` — always `CryptoFill`
- `services/shared/test_*.py` — add discrimination tests
- `services/relays/ibkr/test_flex_parser.py`, `test_ibkr.py` — narrow with `isinstance`
- `services/relays/kraken/test_*.py` — narrow with `isinstance`
- `types/typescript/**` and `types/python/**` — regenerated by `make types`, commit the diff
