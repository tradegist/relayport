---
applyTo: "services/relay_core/**"
---

# `services/relay_core/` — Generic engines + HTTP API

Main Docker container. Provides the generic polling engine, listener engine, HTTP API, and relay registry. Broker-specific logic lives in `services/relays/<name>/`.

## Reliability (cross-reference)

Mark-after-notify, atomic mark+notify boundaries, and SQLite commit discipline are MANDATORY — see the root `.github/copilot-instructions.md`. Most often violated in `poller_engine.py` and `listener_engine.py`.

## Auth Pattern

- API endpoints under `/relays/*` require `Authorization: Bearer <API_TOKEN>` (HMAC-safe via `hmac.compare_digest`).
- **All authenticated routes must use the `AUTH_PREFIX` constant** (from `relay_core.routes.middlewares`) when registering with the router. The auth middleware uses the same constant to decide which requests require a token — hardcoding the path in either place causes them to drift.
- Webhook payloads are signed with HMAC-SHA256 (`X-Signature-256` header) via the notifier package.

## Relay Registry Pattern

The container uses a registry to support multiple broker adapters:

1. `RELAYS` env var lists active relays (`RELAYS=ibkr,kraken`).
2. `registry.py` validates each against `RelayName` (a `Literal` in `shared/models.py`).
3. For each relay, the registry dynamically imports `relays.<name>` and calls `build_relay()`.
4. The adapter returns a `BrokerRelay` dataclass with `PollerConfig`s, `ListenerConfig`, and notifiers.
5. `main.py` starts a poll loop per `PollerConfig` and a WS listener (if configured).

To add a new broker, see the `add-relay-adapter` skill in `.claude/skills/`.

## Engines

- **`poller_engine.poll_once(relay_name, poller_index)`** — resolves `PollerConfig`, notifiers, and retry config from the relay context. Handles two-layer dedup (exec_id + order-level within `2 × interval`), aggregation, notify, mark.
  - **exec_id dedup** (always on): listener writes each fill's `execId` to the shared SQLite dedup DB. Poller skips fills whose `execId` is present. Works when broker uses the same identifier on WS and REST (IBKR — except combo-leg execIds, reconciled by the dedup-alias layer below).
  - **order-level dedup** (listener-side write): listener also stores `orderId`. Poller drops candidates whose `orderId` was processed by the listener within `2 × POLL_INTERVAL`. Catches brokers where REST and WS identifiers differ (Kraken multi-match).
  - **in-flight deferral** (`inflight.py`): the listener holds its orderIds in the process-wide `INFLIGHT_ORDERS` registry for exactly the notify+mark span; the poller defers candidates whose order is registered and caps its timestamp watermark at the earliest deferred fill (advancing past it would let the pre-filter strand the fill if the listener's notify then fails). Closes the window where a slow receiver keeps the listener's notify in flight — nothing marked yet — while a poll cycle runs. Registration is counted (not exclusive) and `threading.Lock`-guarded, since both engines dispatch from `asyncio.to_thread` workers. Deliberately one-directional — the poller never registers: WS events precede REST visibility, and only the poller can defer safely (its fills reappear next cycle; a listener event is consumed once).
  - **dedup aliases** (`BrokerRelay.dedup_aliases`, optional per relay): maps an exec ID from the listener (WS) path to the alternate IDs the broker's other feed uses for the *same* execution (IBKR: TWS reports combo-leg execIds with a 5th segment that Flex truncates to 4). The listener (1) marks alias keys alongside the real exec IDs on successful notify, so the poller's exact-match lookup hits, and (2) on its own read path treats a fill as seen when an alias row exists with `order_id` NULL — i.e. poller-written (covers poller-first ordering: TWS can emit executions hours after Flex reports them). The NULL gate means a listener-written alias row never suppresses a sibling execution truncating to the same alias. Aliases are additive: a wrong alias degrades to a duplicate webhook, never a missed fill. The poller engine needs no alias handling — its statement-side IDs are already the canonical short form.
- **`listener_engine.start_listener(relay_name)`** — resolves `ListenerConfig`, notifiers, retry config. Calls the adapter's `connect` callback to obtain a connected websocket; dispatches via `event_filter` and `on_message`; handles dedup + notify + mark; auto-reconnects with exponential backoff.
- The debounce buffer is **per-orderId**: each orderId has its own quiet-window timer and flushes immediately when a fill arrives with `OnMessageResult.order_complete=True`. Every `mark=True` fill goes through it — with `debounce_ms=0` via `DebounceBuffer.send_now` (one batch per WS message) — so the delivery policy below applies regardless of debounce.
- **Listener delivery policy (MANDATORY) — no fill may stay in the buffer indefinitely.** Fills were once restored to the buffer after a failed send with no timer, and sat there until the next WS drop. That once re-sent 13 months-old orders and exhausted a 10/day webhook quota.
  - **Stale-fill guard** — fills older than `ListenerConfig.max_fill_age_s` (`{RELAY}_LISTENER_MAX_FILL_AGE_HOURS`, default 24h) are dropped via `DebounceBuffer.drop_stale`: on `add()`, again right before every send (`_flush_order` / `send_now`), and for `mark=False` fills. A fill with an unparseable timestamp is kept and logged — a duplicate beats a lost fill.
  - **Failure handling** (`_on_flush_failure`) — a failure `is_transient_failure` (notifier package) accepts (any backend 5xx / timeout / network, or a non-`NotificationError`) schedules a retry after the next `retry_delays_s` delay (`{RELAY}_LISTENER_RETRY_DELAYS_S`, default `60,300,900`). A 4xx from every notifier, or an exhausted budget, drops the fills (never marked) and alerts once per order. Never restore failed fills without a pending timer.
  - **`flush()`** (disconnect / shutdown) skips orders waiting out a retry backoff, and the disconnect flush runs as a background task so it never delays the reconnect.
  - **Duplicate execIds are merged** (`_merge_fills`, newer copy wins) on `add()` and on restore — two copies would double the aggregated volume.
- **WS close logging** — `async for msg in ws` ends silently when the connection closes (aiohttp turns CLOSE frames into `StopAsyncIteration`), so `_listen` logs the close code in the loop's `else:` branch.
- On successful notify the listener writes `execId` (plus any relay-declared dedup aliases) and `orderId` to the shared dedup DB.
- The `connect` callback owns the connection protocol (auth, subscription). The engine only manages the message loop and reconnection.
- **Bridge WS resume cursor** — `get_last_bridge_seq` / `get_last_bridge_id` / `set_bridge_cursor` in `poller_engine.py` persist the last delivered bridge sequence number **and the `bridgeId` that issued it** in the metadata DB. The keys are `{relay}:bridge_last_seq` and `{relay}:bridge_id`, written in one transaction. The adapter's `connect` callback reads the cursor on startup, sends it back as `?last_seq=…&bridge_id=…`, and updates it on every message via `asyncio.to_thread`. ibkr_bridge restarts `seq` at 1 in every process, so an event from a new `bridgeId` resets the cursor to that event's (lower) seq. A mismatched `bridge_id` on reconnect makes the bridge replay its own buffer, and without `last_seq` the bridge replays nothing. Falls back gracefully to in-memory-only tracking when the metadata DB is unavailable.

## Context (singleton)

- **`context.init_relays(relays)`** is called once at startup by `amain()`. Then `get_relay(name)` and `get_relays()` are available anywhere to access relay config (notifiers, retry config, poller/listener configs) without parameter threading.
- `_reset()` is exposed for test teardown.
- Uses `TYPE_CHECKING` guard for `BrokerRelay` to avoid circular import with `__init__.py`.

## Env helpers

- **`relay_core.env.get_env(var, prefix, suffix, default)`** and **`get_env_int(...)`** — resolution order: `{prefix}{var}{suffix}` → `{var}{suffix}` → `default`.
- All relay-core env var readers (`get_poll_interval`, `get_debounce_ms`, `load_retry_config`, notifier env loading) use these helpers. When adding new env var readers, use them rather than writing inline `os.environ.get()` with manual fallback.

## Notifier Package (`relay_core/notifier/`)

- **`NOTIFIERS` env var** controls active backends (`NOTIFIERS=webhook`). Empty = no notifications (dry-run).
- **Prefix support** — adapters pass a prefix (`IBKR_`) to read from `IBKR_TARGET_WEBHOOK_URL`, etc. Enables per-relay destinations.
- **Suffix support** — `_2` suffixed vars enable separate destinations for multi-account pollers within a single relay.
- **Validation belongs in each notifier's `__init__`, not the coordinator.** The coordinator (`__init__.py`) is a registry + dispatcher. Each `BaseNotifier` subclass validates its own env vars in its constructor and raises `SystemExit` on misconfiguration.
- **`validate_notifier_env()`** is called by `cli/__init__.py` during pre-deploy checks. Instantiates each configured backend, converts `SystemExit` to `die()`.
- **Adding a new backend** — create `services/relay_core/notifier/<name>.py` extending `BaseNotifier`, add to `REGISTRY` in `__init__.py`. Constructor must validate all required env vars.
- **`deliveryId` / `X-Delivery-Id`** — `WebhookPayloadTrades` auto-computes a content-derived dedup key via a model validator (hash of relay + each trade's `orderId` + sorted `execIds`; error strings when the payload has no trades) — construction sites never pass it, and retries/re-sends of the same trades keep the same value. `WebhookNotifier` exposes it as the `X-Delivery-Id` header. Derive it from broker-assigned identity fields only — never from price/volume/timestamp, which can legitimately collide across distinct trades and would make a receiver silently drop a real one.
- **Webhook timeout** — `_TIMEOUT` in `webhook.py` gives reads a 30s budget (10s for connect/write/pool). Receivers that ack only after processing, or that cold-start, can exceed 10s on successful deliveries — and every false-negative timeout becomes a duplicate re-send. Don't tighten it without revisiting the delivery-semantics section in the README.
- **Engines resolve notifiers from the relay context** — loaded once at startup per relay, stored on `BrokerRelay`, accessed via `get_relay(name).notifiers`.
- **Debug webhook URL resolution** — `WebhookNotifier.__init__` calls `_resolve_webhook_url()`. If `DEBUG_WEBHOOK_PATH` is set, URL is overridden to `http://debug:9000/debug/webhook/{path}` (container DNS). Otherwise reads `TARGET_WEBHOOK_URL`. No env var mutation — resolved URL stored in `self._url`.
- **Fill audit log** — `audit.py` writes a rotating JSONL file at `/data/logs/fills_audit.jsonl` (daily rotation, 7-day retention; override via `FILL_AUDIT_LOG_PATH`). Two functions: `log_fills(relay_name, fills)` called from both engines before the dedup step (captures every fill considered including future duplicates), and `log_payload(relay_name, payload)` called inside `notify()` before dispatch (captures the aggregated `WebhookPayloadTrades`). Uses `@functools.cache` to initialise once; silently disabled when the log directory cannot be created.

## Dedup Package (`relay_core/dedup/`)

- Owns the SQLite schema. `processed_fills` has three columns: `exec_id TEXT PRIMARY KEY`, `order_id TEXT` (NULL for poller-written rows; populated for listener-written), `processed_at`.
- **Retention** — `prune()` runs once at startup with `get_retention_days()` (`DEDUP_RETENTION_DAYS`, default 90). Keep it well beyond any feed's replay horizon: a replayed fill whose row was pruned looks new and is re-sent.
- `init_db` performs an idempotent `ALTER TABLE` migration (see PRAGMA-gated pattern in root rules).
- Two write paths: `mark_processed_batch` (exec_id only, poller) and `mark_processed_batch_with_orders` (listener).
- Three read paths: `get_processed_ids` (exec_id set lookup, poller), `get_processed_rows` (exec_id → `order_id` mapping — the listener uses the NULL/non-NULL distinction to gate dedup-alias hits), and `get_recently_processed_order_ids` (relay-prefixed + time-windowed; ignores NULL-order_id rows so poller-only marks never block subsequent polls).
- **Dedup key priority**: `ibExecId → transactionId → tradeID`, resolved in `services/relays/ibkr/flex_parser.py` at parse time by setting `Fill.execId`. The engines then dedup directly on `fill.execId` — there is no helper indirection.
- **Book-trade cross-path dedup** (third layer): fills the broker books outside normal execution flow (option assignment/exercise/expiry) carry disjoint identifiers on the two paths, so both identifier layers miss them. Adapters that can classify them provide `BrokerRelay.book_trade_key: Callable[[Fill], str | None]` (None = feature off — Kraken); the engines reconcile via `consume_book_trade_keys` / `mark_book_trade_keys`. Semantics that MUST be preserved:
  - **Source-tagged keys** — rows are `{relay}:bt:{poll|ws}:{key}` and a fill only consumes a key written by the *opposite* engine. Same-path repeats of identical events (partial assignment in equal tranches on consecutive days, single-path config) must always notify.
  - **Consume is a single-statement DELETE with a rowcount check** — atomic across the two engine connections (no SELECT-then-DELETE TOCTOU), and naturally multiset: N identical-key fills against one stored row consume exactly one.
  - **Consumed fills are exec-marked without notifying** — a documented exception to mark-after-notify, legitimate only because keys are inserted strictly AFTER a successful notify, so a consumable key proves prior delivery of identical content.
  - **Key insertion uses `INSERT OR REPLACE`** so a stale same-key row gets a fresh `processed_at`; `BOOK_TRADE_WINDOW_SECONDS` (18 h) stays strictly under the ~24 h cadence of consecutive-night assignment tranches. Every failure mode (missing marker, expired window, concurrent race) degrades to a duplicate webhook, never a dropped fill.
  - **Keys embed the account id — never log them.** Log symbol/side/volume only.
- The poller engine has a separate metadata DB at `META_DB_PATH` (default `/data/meta/relay.db`) on a `relay-meta` volume. It stores three key types: `{relay}:last_poll_ts` (timestamp watermark), `{relay}:bridge_last_seq` and `{relay}:bridge_id` (bridge WS resume cursor). See `get_last_poll_ts` / `set_last_poll_ts` and `get_last_bridge_seq` / `get_last_bridge_id` / `set_bridge_cursor` in `poller_engine.py`.

## Routes (`relay_core/routes/`)

- `GET /health` — unauthenticated, health check.
- `POST /relays/{relay_name}/poll/{poll_idx}` — authenticated (Bearer `API_TOKEN`), 1-based index.
