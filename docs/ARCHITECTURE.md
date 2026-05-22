# RelayPort — Architecture

Descriptive overview of how the system fits together. **Enforceable rules** live in `CLAUDE.md` files (root + per-directory) and their Copilot mirrors. This document is reference material for humans and for Claude when asked architectural questions — it is not auto-loaded into every Claude conversation.

## System overview

Four Docker containers in a single Compose stack on a DigitalOcean droplet (debug is optional):

| Service | Role |
| --- | --- |
| `caddy` | Reverse proxy with automatic HTTPS (Let's Encrypt) |
| `relays` | Multi-relay service: loads broker adapters via the registry, runs pollers + listeners + HTTP API. Disabled when `RELAYS` is empty (API server still runs for health checks) |
| `market_data` | Market data lookup service: REST API for dividend info via Yahoo Finance. Port 8001. Protected by its own `MD_API_TOKEN` |
| `debug` | Debug webhook inbox — captures webhook payloads for inspection. Disabled by default (`DEBUG_REPLICAS=0`), enabled when `DEBUG_WEBHOOK_PATH` is set |

## Relay registry pattern

The `relays` container uses a registry to support multiple broker adapters:

1. `RELAYS` env var lists active relays (e.g. `RELAYS=ibkr`, `RELAYS=ibkr,kraken`).
2. `registry.py` validates each name against `RelayName` (a `Literal` type in `shared/models.py`).
3. For each relay, the registry dynamically imports `relays.<name>` and calls `build_relay()`.
4. The adapter returns a `BrokerRelay` dataclass with `PollerConfig`s, `ListenerConfig`, and notifiers.
5. `main.py` starts a poll loop per `PollerConfig` and a WS listener per relay (if configured).

To add a new broker adapter, see the [`add-relay-adapter`](../.claude/skills/add-relay-adapter/SKILL.md) skill (full 11-step procedure).

## Project file structure

```
env_examples/              # Env var templates (make setup copies to .<name>)
  env                      # → .env (app config)
  env.droplet              # → .env.droplet (CLI-only deployment config)
  env.relays               # → .env.relays (relay-prefixed vars)
  env.test                 # → .env.test (E2E test config)
docker-compose.yml             # All services (caddy, relays, debug, market_data)
docker-compose.shared.yml      # Shared-mode overlay (disables Caddy)
docker-compose.shared-network.yml # Marks SHARED_NETWORK as external
docker-compose.local.yml   # Local dev override (direct port access, no TLS)
docker-compose.test.yml    # Test stack override (env_file: !override with .env.test)
cli/                       # Python CLI (operator scripts, stdlib only)
  __init__.py              # Shared helpers (env loading, SSH, DO API, validation)
  __main__.py              # Entry point (lazy dispatch via importlib)
  core/
    deploy.py destroy.py pause.py resume.py sync.py
  poll.py                  # Trigger immediate poll (relay + index)
  test_webhook.py          # Send test webhook payload
  watermark.py             # Reset poll timestamp watermark
services/                  # Business-logic services
  relay_core/              # Main container: registry + engines + HTTP API
    __init__.py            # BrokerRelay dataclass, re-exports engine types
    main.py                # Loads relays, starts pollers + listeners + API
    context.py             # Relay context singleton
    env.py                 # get_env() / get_env_int() helpers
    registry.py            # Relay registry (RELAYS → adapter loading)
    poller_engine.py       # Generic poller (dedup, fetch, parse, notify, mark)
    listener_engine.py     # Generic WS listener (connect, dedup, notify, reconnect)
    relay_models.py        # Re-export shim (shared + RunPollResponse, HealthResponse)
    dedup/                 # SQLite dedup library
    notifier/              # Pluggable notification backends (BaseNotifier, webhook.py)
    routes/                # HTTP API (handle_health, handle_poll, auth middleware)
    tests/e2e/             # E2E tests (smoke + listener) + conftest with two-tier preflight
  relays/                  # Broker adapters (one package per broker)
    ibkr/                  # IBKR adapter (Flex XML poll + bridge WS listener)
      __init__.py          # build_relay()
      bridge_models.py     # Mirrored WsEnvelope types from ibkr_bridge
      flex_fetch.py        # Two-step Flex Web Service fetch (pure library)
      flex_dump.py         # CLI entrypoint (fetch + write)
      flex_parser.py       # Flex XML parser (Activity + Trade Confirmation)
      timestamps.py        # flex_to_iso(), bridge_to_iso() helpers
      fixtures/
        sanitize.py        # Sanitize raw dump → committable fixture
        activity_flex_sample.xml
        trade_confirm_sample.xml
    kraken/                # Kraken crypto exchange adapter
      __init__.py          # build_relay() — shared KrakenClient
      rest_client.py       # KrakenClient (HMAC-SHA512 auth, nonce lock)
      ws_parser.py         # WS v2 executions channel parser
      kraken_types.py      # TypedDicts for raw API shapes
  shared/                  # Shared models + utilities (no container)
    models.py              # Fill, Trade, BuySell, RelayName, etc.
    utilities.py           # aggregate_fills, normalize_*, _dedup_id
    time_format.py         # normalize_timestamp() — ISO-8601 → canonical UTC
  debug/                   # Debug webhook inbox service
    debug_app.py
  market_data/             # Market data HTTP service (Yahoo Finance dividends)
    main.py errors.py utils.py
    adapters/              # MarketDataAdapter registry (singleton caching)
      yahoo.py             # YahooAdapter: YahooClient → DividendsUpcomingItem
    models/dividends.py    # DividendsUpcoming{Query,Item,Response}, TickerError
    routes/                # app.py, dividends.py, middlewares.py
    yahoo_client/          # curl_cffi + Chrome TLS impersonation
infra/
  caddy/Caddyfile          # Reverse proxy config (uses {$SITE_DOMAIN})
  caddy/sites/             # Route snippets imported inside {$SITE_DOMAIN}
    relayport.caddy        # /relays/* → relays:8000
    debug.caddy            # /debug/webhook/* → debug:9000
    market_data.caddy      # /v1/market-data/* → market_data:8001
types/
  typescript/              # @tradegist/relayport-types (BrokerRelay + RelayApi + MarketDataApi)
  python/                  # relayport-types PyPI package
schema_gen.py              # JSON Schema generator (Pydantic → JSON Schema)
gen_ts_barrels.py          # TS barrel generator
gen_python_types.py        # Python types generator
terraform/                 # Infrastructure as code (DigitalOcean)
```

## `services/relay_core/` package details

- **`main.py`** — reads `RELAYS`, loads adapters via the registry, initialises the relay context (`init_relays()`), starts the HTTP API, then spawns a poll loop per `PollerConfig` and a WS listener per relay (if configured). When `RELAYS` is empty, the API server starts alone (for health checks).
- **`context.py`** — relay context singleton. `init_relays(relays)` is called once at startup. `get_relay(name)` / `get_relays()` are available anywhere to access relay config (notifiers, retry config, poller/listener configs) without parameter threading.
- **`poller_engine.poll_once(relay_name, poller_index)`** — resolves `PollerConfig`, notifiers, and retry config from the relay context. Handles two-layer dedup (exec_id + order-level within `2 × interval`), aggregation, notify, and mark.
- **`listener_engine.start_listener(relay_name)`** — generic WS listener with per-orderId debounce buffer and auto-reconnect with exponential backoff.
- **`dedup/__init__.py`** — owns the SQLite schema. Three columns on `processed_fills`: `exec_id` (PK), `order_id`, `processed_at`. Idempotent `ALTER TABLE` migration on `init_db`.
- **`relay_models.py`** — re-export shim for notifier payload contracts + relay-specific API types (`RunPollResponse`, `HealthResponse`). Listed in `schema_gen.py:SCHEMA_MODELS` under `"relay_core.relay_models"`.
- **`routes/__init__.py`** — `GET /health` (unauthenticated) and `POST /relays/{relay_name}/poll/{poll_idx}` (authenticated, 1-based index).
- **`env.py`** — `get_env(var, prefix, suffix, default)` and `get_env_int(...)`. Resolution order: `{prefix}{var}{suffix}` → `{var}{suffix}` → `default`.

## `services/relays/` adapter details

Each adapter is a small package that wires broker-specific logic into the generic engines. The only required contract is `build_relay(notifiers) -> BrokerRelay`. See per-adapter `CLAUDE.md` files for specifics (IBKR uses Flex XML + bridge WS; Kraken uses REST + native WS v2). Cross-cutting conventions (fee normalisation, timestamp normalisation, option contracts) live in [services/relays/CLAUDE.md](../services/relays/CLAUDE.md).

## Notifier package

- **`NOTIFIERS` env var** controls active backends (`NOTIFIERS=webhook`). Empty = no notifications (dry-run).
- **Prefix support** — adapters pass `IBKR_` to read from `IBKR_TARGET_WEBHOOK_URL`, etc. Enables per-relay destinations.
- **Suffix support** — `_2` suffixed env vars enable separate destinations for multi-account pollers.
- Adding a new backend: create `services/relay_core/notifier/<name>.py` with a class extending `BaseNotifier`, add to `REGISTRY`. The constructor must validate all required env vars and raise `SystemExit` on misconfiguration.

## Dedup package

- `processed_fills` schema: `exec_id TEXT PRIMARY KEY`, `order_id TEXT` (NULL for poller-written rows; populated for listener-written rows), `processed_at`.
- Write paths: `mark_processed_batch` (exec_id only, poller) and `mark_processed_batch_with_orders` (listener).
- Read paths: `get_processed_ids` (exec_id set lookup) and `get_recently_processed_order_ids` (relay-prefixed + time-windowed, ignores NULL-order_id rows).
- Dedup key priority: `ibExecId → transactionId → tradeID`, resolved in `services/relays/ibkr/flex_parser.py` at parse time.

## Models — three locations

| File | Domain | Contains |
| --- | --- | --- |
| `services/shared/models.py` | CommonFill primitives | `Fill`, `Trade`, `OptionContract`, `BuySell`, `AssetClass`, `OrderType`, `Source`, `RelayName` |
| `services/relay_core/notifier/models.py` | Notifier payload (outbound) | `WebhookPayloadTrades`, `WebhookPayload` |
| `services/relay_core/relay_models.py` | Relay API (outbound) | Re-exports notifier payload + `RunPollResponse`, `HealthResponse` |

`shared/models.py` (key `"shared"`) and `relay_core/relay_models.py` (key `"relay_core.relay_models"`) are both listed in `schema_gen.py:SCHEMA_MODELS` so they regenerate via `make types`.

## Env file flow

```
.env         ─┐
.env.relays  ─┤── env_file: in docker-compose.yml ──▶ relays container
              │
.env.droplet ─── CLI only (never pushed to container)
.env.test    ─── env_file: !override in docker-compose.test.yml ──▶ test containers
```

All secrets are injected via `env_file:` in `docker-compose.yml`. Caddy reads `SITE_DOMAIN` from its `environment:` block — the Caddyfile uses `{$SITE_DOMAIN}` syntax.

## Deployment modes

Controlled by `DEPLOY_MODE` in `.env.droplet`:

- **Standalone** — Terraform creates a fresh droplet + firewall + reserved IP; CLI rsyncs + pushes env files + brings the stack up.
- **Shared** — Multiple projects share a single droplet and a single Caddy. Set `SHARED_NETWORK` in `.env`. CLI applies `docker-compose.shared.yml` (disables Caddy) + `docker-compose.shared-network.yml` (joins the external network).

See [cli/CLAUDE.md](../cli/CLAUDE.md) for the full deploy/sync rules and rsync invariants.

## Authentication

- `/relays/*` endpoints require `Authorization: Bearer <API_TOKEN>`.
- `/v1/market-data/*` endpoints require `Authorization: Bearer <MD_API_TOKEN>` (separate token).
- HMAC-safe comparison via `hmac.compare_digest`.
- Webhook payloads are signed with HMAC-SHA256 (`X-Signature-256` header).

## Local development

- `.venv` is the project's virtual environment (`make setup` creates it from Homebrew Python).
- `relayport.pth` adds `services/debug/`, `services/`, `services/relay_core/` to `sys.path`.
- `docker-compose.local.yml` adds `:ro` bind mounts so local source shadows the image's COPY'd files — code changes are visible on container restart, no rebuild.
- `DEFAULT_CLI_ENV` in `.env.droplet` selects local vs prod for `make sync` and `make logs`.

## Auto-loaded vs on-demand instructions (this repo's setup)

This repo splits AI instruction files into three layers:

| Layer | Location | When loaded by Claude |
| --- | --- | --- |
| **Always-on rules** | Root `CLAUDE.md` | Every session |
| **Directory-scoped rules** | `<dir>/CLAUDE.md` | On demand, when Claude reads a file in that subtree |
| **Playbooks** | `.claude/skills/<name>/SKILL.md` | Only when the skill is invoked |
| **Architecture prose** (this doc) | `docs/ARCHITECTURE.md` | Not auto-loaded — read on demand via Read/Grep |

The same split is mirrored to GitHub Copilot via `.github/copilot-instructions.md` (universal) and `.github/instructions/*.instructions.md` (path-scoped with `applyTo:` frontmatter). See [docs/INSTRUCTION_FILES.md](INSTRUCTION_FILES.md) for the maintenance contract.
