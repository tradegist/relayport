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
  - **exec_id dedup** (always on): listener writes each fill's `execId` to the shared SQLite dedup DB. Poller skips fills whose `execId` is present. Works when broker uses the same identifier on WS and REST (IBKR).
  - **order-level dedup** (listener-side write): listener also stores `orderId`. Poller drops candidates whose `orderId` was processed by the listener within `2 × POLL_INTERVAL`. Catches brokers where REST and WS identifiers differ (Kraken multi-match).
- **`listener_engine.start_listener(relay_name)`** — resolves `ListenerConfig`, notifiers, retry config. Calls the adapter's `connect` callback to obtain a connected websocket; dispatches via `event_filter` and `on_message`; handles dedup + notify + mark; auto-reconnects with exponential backoff.
- The debounce buffer is **per-orderId**: each orderId has its own quiet-window timer and flushes immediately when a fill arrives with `OnMessageResult.order_complete=True`.
- On successful notify the listener writes both `execId` and `orderId` to the shared dedup DB.
- The `connect` callback owns the connection protocol (auth, subscription). The engine only manages the message loop and reconnection.

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
- **Engines resolve notifiers from the relay context** — loaded once at startup per relay, stored on `BrokerRelay`, accessed via `get_relay(name).notifiers`.
- **Debug webhook URL resolution** — `WebhookNotifier.__init__` calls `_resolve_webhook_url()`. If `DEBUG_WEBHOOK_PATH` is set, URL is overridden to `http://debug:9000/debug/webhook/{path}` (container DNS). Otherwise reads `TARGET_WEBHOOK_URL`. No env var mutation — resolved URL stored in `self._url`.

## Dedup Package (`relay_core/dedup/`)

- Owns the SQLite schema. `processed_fills` has three columns: `exec_id TEXT PRIMARY KEY`, `order_id TEXT` (NULL for poller-written rows; populated for listener-written), `processed_at`.
- `init_db` performs an idempotent `ALTER TABLE` migration (see PRAGMA-gated pattern in root rules).
- Two write paths: `mark_processed_batch` (exec_id only, poller) and `mark_processed_batch_with_orders` (listener).
- Two read paths: `get_processed_ids` (exec_id set lookup) and `get_recently_processed_order_ids` (relay-prefixed + time-windowed; ignores NULL-order_id rows so poller-only marks never block subsequent polls).
- **Dedup key priority**: `ibExecId → transactionId → tradeID`, resolved in `services/relays/ibkr/flex_parser.py` at parse time by setting `Fill.execId`. The engines then dedup directly on `fill.execId` — there is no helper indirection.
- The poller engine has a separate metadata DB at `META_DB_PATH` (default `/data/meta/<relay>.db`) on a `relay-meta` volume for the timestamp watermark.

## Routes (`relay_core/routes/`)

- `GET /health` — unauthenticated, health check.
- `POST /relays/{relay_name}/poll/{poll_idx}` — authenticated (Bearer `API_TOKEN`), 1-based index.
