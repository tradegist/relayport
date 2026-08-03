# `services/relays/` — Broker adapters (cross-cutting rules)

Each broker adapter is a small package that wires broker-specific logic into the generic `relay_core` engines. The only required contract is `build_relay(notifiers: list[BaseNotifier]) -> BrokerRelay`.

For the full step-by-step procedure to add a new broker, see the [`add-relay-adapter`](../../.claude/skills/add-relay-adapter/SKILL.md) skill.

## Sign convention (volume / cost / fee)

RelayPort departs from the FIX/exchange norm of "unsigned magnitude + side flag": `volume` and `cost` are **signed deltas**, so consumers fold trades into positions with a plain `SUM`.

| Field    | Meaning                      | Buy      | Sell     |
| -------- | ---------------------------- | -------- | -------- |
| `volume` | Position delta (units)       | positive | negative |
| `cost`   | Cash delta (currency)        | negative | positive |
| `fee`    | Amount paid (always a debit) | positive | positive |
| `price`  | Unit price (never signed)    | positive | positive |

**Adapters must NOT sign anything.** Forward the broker's native `volume` / `cost` untouched — a `model_validator` on `Fill` and `Trade` (`_apply_sign_convention` in [services/shared/models.py](../shared/models.py)) reduces each value to its magnitude and re-signs it from `side`. It is idempotent, so an already-correctly-signed broker value passes through unchanged.

This is centralised precisely because brokers disagree, and not only about *whether* they sign:

- **IBKR Flex** signs `quantity` our way (buy +, sell −) but reports `cost` as a **cost-basis** delta (buy +, sell −) — inverted relative to a cash delta.
- **IBKR bridge** (`ib_async`), **Kraken REST**, and **Kraken WS** all report unsigned magnitudes with direction only on the side field.

A regression here is invisible to mypy and to buy-only testing — the two IBKR paths silently disagreed on sell volume in production until a `sell` shipped with a positive volume. When adding an adapter, **always add a sell-side test asserting `volume < 0` and `cost > 0`**; a buy-only fixture passes under either convention.

`price` is never signed — direction lives on volume/cost, and signing price too would double-count it and corrupt the VWAP weighting in `aggregate_fills` (which weights by `abs(volume)` for exactly this reason).

## Fee normalisation convention

When mapping a broker fill to a `Fill` model, use this priority order for the `fee` field:

1. **Prefer a pre-converted equivalent field** if the broker provides one (e.g. Kraken's `fee_usd_equiv`). Meaningful regardless of how many fee currencies are involved.
2. **Single-asset fallback** — if the broker provides a `fees` array, only aggregate entries when every entry shares the same `asset`. Summing across different assets (USD + BTC) produces a number in no real currency; return `0.0` instead.
3. **`abs()` per entry, not on the total** — fee quantities may be signed. Apply `abs(qty)` to each entry before summing, not `abs(sum(...))` at the end. `abs(-5 + 3) = 2` understates; `abs(-5) + abs(3) = 8` is correct.
4. Return `0.0` when no fee information is available.

Reference: [services/relays/kraken/ws_parser.py](kraken/ws_parser.py) (`_extract_fee`).

## Timestamp normalisation convention

Every `Fill.timestamp` reaching the engine **must** be canonical: `YYYY-MM-DDTHH:MM:SS` — always UTC, no `Z` suffix, no `+00:00`, no fractional seconds. Lexicographic order equals chronological order (relied on by the poll-watermark comparison in `poller_engine.py`).

The pipeline has two layers with a strict split:

1. **Relay-owned** — broker-specific format → ISO-8601. Lives in `services/relays/<name>/timestamps.py`. One small function per native format, each using `strptime` to validate strictly and return a naive ISO-8601 string.
2. **Shared** — ISO-8601 → canonical UTC. Lives in `services/shared/time_format.py::normalize_timestamp(iso, *, assume_tz=None)`. Applies `assume_tz` to naive inputs, converts tz-aware to UTC, strips fractional seconds. **Only** accepts ISO-8601 — never teach it about broker formats.

Call sites chain the two:

```python
ts = normalize_timestamp(flex_to_iso(raw), assume_tz=tz)    # IBKR Flex
ts = normalize_timestamp(bridge_to_iso(raw), assume_tz=tz)  # IBKR bridge
ts = normalize_timestamp(raw)                               # Kraken (already ISO)
```

**Why the split matters.** `datetime.fromisoformat` in 3.12+ is very lenient (accepts IBKR-style `YYYYMMDD-HH:MM:SS` and `YYYYMMDD;HHMMSS` directly). The relay-level helper is a **validation gate** — it rejects typos and wrong separators that `fromisoformat` would silently misinterpret. Without the split, `shared/time_format.py` would become a junk drawer of broker quirks.

**Timezone handling.** Brokers that emit naive timestamps (IBKR Flex, IBKR bridge) need a `{RELAY}_ACCOUNT_TIMEZONE` env var (e.g. `IBKR_ACCOUNT_TIMEZONE=America/New_York`). Read it via a getter that calls `shared.parse_timezone(name)` and converts `ValueError` to `SystemExit` at boot. The resulting `ZoneInfo` is threaded into parse callbacks via `build_relay()` (closure-capture, not re-read per fill). Brokers that emit tz-aware timestamps (Kraken with `Z`) don't need an env var — `normalize_timestamp` ignores `assume_tz` when the input is tz-aware.

## Option contracts

If the broker supports option derivatives, populate `Fill.option` (type `OptionContract`) when `assetClass == "option"`, and leave it `None` otherwise. Fields:

- `rootSymbol: str` — the underlying ticker.
- `strike: float` — strike price.
- `expiryDate: str` — expiry in ISO `YYYY-MM-DD`. Use `flex_date_to_iso()` (or broker equivalent).
- `type: Literal["call", "put"]` — derived from the broker's put/call indicator.

**Never emit a fill with `assetClass == "option"` when option metadata is missing or invalid** — skip the row and surface a parse error instead. An incomplete `option` is worse than a missing fill.

## Listener/poller dedup interaction

When both `LISTENER_ENABLED` and `POLLER_ENABLED` are true for the same relay, the same fill can reach the consumer through both paths. The engine reconciles in three layers (see [services/relay_core/CLAUDE.md](../relay_core/CLAUDE.md) for the implementation): exec_id dedup (always on), order-level dedup (listener-side write, 2× POLL_INTERVAL window), and in-flight deferral (the poller defers orders whose listener notify is still in progress — covers the window before anything is marked).

When designing a new relay, verify experimentally whether the broker reuses identifiers across paths. If not (Kraken-style), the order-level dedup will suppress the poller's fee-bearing webhook for that order — document the fee trade-off in the README. If your broker's listener does not reliably include fees in real time, consider recommending poller-only mode (with a shorter `{RELAY}_POLL_INTERVAL`) as the fee-accurate option.

### Book trades (assignment / exercise / expiry)

Events the broker books outside normal execution flow carry **disjoint identifiers on both paths** (verified for IBKR: Flex has no `ibExecID` and synthesizes per-leg order ids; the bridge reports native exec ids via overnight reconcile), so both identifier layers miss them and the consumer gets a duplicate webhook. If your broker has such events, provide `BrokerRelay.book_trade_key(fill) -> str | None`:

- **Classify from an explicit broker marker only** (IBKR: Flex `transactionType="BookTrade"` or exact-token `notes`/`code` codes; bridge `isBookTrade` envelope flag). Never infer from heuristics like sentinel field values — a misclassified normal fill enters the dedup layer and can suppress real trades.
- **Key format**: `{account}|{symbol}|{side}|{abs(volume):.10g}|{price:.10g}`. Account-scoped (multi-account configs must not cross-consume) — therefore never log the key.
- Return `None` when unclassified OR when the account cannot be resolved — always fail open to a duplicate, never guess.
- Engine semantics (consume-once, source-tagged, 18 h window) live in `relay_core/dedup` — see [services/relay_core/CLAUDE.md](../relay_core/CLAUDE.md).
