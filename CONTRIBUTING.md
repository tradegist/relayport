# Contributing to BrokeRelay

Developer and contributor reference — setup, testing, project structure, type regeneration, and broker-adapter internals.

For deployment and user-facing documentation, see the [README](README.md).

## Table of Contents

- [Commands](#commands)
- [Testing](#testing)
- [IBKR Fixtures](#ibkr-fixtures)
- [TypeScript Types](#typescript-types)
- [Python Types](#python-types)
- [Project Structure](#project-structure)
- [Flex XML Parsing](#flex-xml-parsing)
- [IBKR ID Reference](#ibkr-id-reference)

## Commands

All operations are available via `make` or the Python CLI directly. Run `make help` to see the full list:

```
  make deploy            Deploy infrastructure (Terraform + Docker)
  make destroy           Permanently destroy all infrastructure
  make pause             Snapshot droplet + delete (save costs)
  make resume            Restore droplet from snapshot
  make setup             Create .venv and install all dependencies
  make sync              Push .env + .env.relays + restart (S=service B=1 LOCAL_FILES=1 ENV=local)
  make poll              Trigger an immediate poll (RELAY=ibkr IDX=1 V=1 REPLAY=N)
  make ibkr-flex-dump    Download and print the current IBKR Flex report
  make ibkr-flex-refresh Request a fresh IBKR Flex report, then download it
  make test-webhook      Send sample trades to webhook endpoint
  make types             Regenerate TypeScript + Python types from Pydantic models
  make test              Run unit tests (pytest)
  make typecheck         Run mypy strict type checking
  make lint              Run ruff linter (FIX=1 to auto-fix)
  make e2e               Run E2E tests (starts/stops stack automatically)
  make e2e-up            Start E2E test stack (relays + debug)
  make e2e-run           Run E2E tests (stack must be up)
  make e2e-down          Stop and remove E2E test stack
  make local-up          Start full stack locally (no TLS, direct port access)
  make local-down        Stop local stack
  make logs              Stream logs (S=service ENV=local, default: relays on droplet)
  make stats             Show container resource usage
  make ssh               SSH into the droplet
  make help              Show available commands
```

Most targets map 1:1 to `python3 -m cli <command>` — useful on Windows where `make` isn't available. Exceptions: `ibkr-flex-dump` and `ibkr-flex-refresh` invoke `python -m relays.ibkr.flex_dump` directly and have no `cli` equivalent.

> [!NOTE]
> `make sync LOCAL_FILES=1` requires `rsync` and SSH, which are only native on macOS and Linux. On Windows, use [WSL](https://learn.microsoft.com/en-us/windows/wsl/).

Usage examples showing the parameter variants:

```bash
make sync                                      # push .env + .env.relays + restart all services
make sync S=relays                             # push env + restart one service
make sync B=1                                  # push env + rebuild images + restart
make sync LOCAL_FILES=1                        # rsync files + rebuild + restart (full deploy)
make sync LOCAL_FILES=1 S=relays               # full deploy, rebuild only relays
make poll                                      # trigger immediate Flex poll (IBKR, primary)
make poll RELAY=ibkr IDX=2                     # trigger second account poller
make poll V=1                                  # verbose (stream container logs)
make poll REPLAY=3                             # resend 3 trades (skip dedup)
make test-webhook                              # send 3 sample trades to webhook
make test-webhook S=2                          # send to second webhook
make logs                                      # stream relays logs (droplet)
make logs S=debug                              # stream debug inbox logs
make logs ENV=local                            # stream local stack logs
```

### Which service to sync

After changing a variable in `.env` or `.env.relays`, restart only the affected service:

| Variable                                        | Service | Command              |
| ----------------------------------------------- | ------- | -------------------- |
| `API_TOKEN`, relay vars, webhook vars, `RELAYS` | relays  | `make sync S=relays` |
| `SITE_DOMAIN`                                   | caddy   | `make sync S=caddy`  |
| Multiple services or unsure                     | all     | `make sync`          |

### Syncing code changes

#### Local stack

When `DEFAULT_CLI_RELAY_ENV=local` (or `ENV=local`), `make sync` simply restarts all containers. Bind mounts in `docker-compose.local.yml` ensure your code changes are picked up automatically — no rebuild needed:

```bash
make sync              # restart containers (when DEFAULT_CLI_RELAY_ENV=local)
make sync ENV=local    # explicit override
```

#### Remote droplet

`make sync` only pushes `.env` + `.env.relays` and restarts containers — it does **not** update source files on the droplet. When you change Python code, Dockerfiles, or Compose config, use `LOCAL_FILES=1` to sync everything:

```bash
make sync LOCAL_FILES=1
```

This runs a full pre-deploy pipeline before anything reaches the droplet:

1. Verify you're on `main` (aborts on feature branches)
2. Verify working tree is clean (aborts on uncommitted changes)
3. `make typecheck` — mypy strict type checking
4. `make test` — all unit tests
5. `rsync` project files to the droplet (respects `.gitignore`, excludes `.env`, `.env.relays`, `.env.droplet`)
6. Push `.env` + `.env.relays`
7. `docker compose up -d --build --force-recreate`

If any step fails, the deploy aborts — nothing reaches the droplet.

If you forked this repo, pull upstream changes first, then deploy:

```bash
git pull upstream main   # merge latest changes from upstream
make sync LOCAL_FILES=1  # deploy to your droplet
```

## Testing

```bash
make test        # run pytest (all unit tests)
make typecheck   # strict mypy checking
make lint        # run ruff linter
```

### E2E tests

E2E tests run against a local Docker stack (`docker-compose.test.yml`) with the relays and debug webhook services.

```bash
make e2e          # start stack → run tests → stop stack
make e2e-up       # start test stack (idempotent)
make e2e-run      # run E2E tests (stack must be up)
make e2e-down     # stop and remove test stack
```

- Credentials live in `.env.test` (gitignored). Template: `env_examples/env.test`.
- `make e2e-run` restarts `relays` and `debug` containers to pick up code changes from volume mounts, then runs the E2E tests. Safe to call repeatedly — no rebuild needed.
- Test relays service runs on `localhost:15011` with token `test-token`.

#### Listener E2E tests

Listener E2E tests are **opt-in** — they require a running [ibkr_bridge](https://github.com/tradegist/ibkr_bridge) local stack and additional `.env.test` variables:

```env
LISTENER_ENABLED=true
IBKR_BRIDGE_WS_URL=ws://host.docker.internal:15101/ibkr/ws/events
IBKR_BRIDGE_API_BASE_URL=http://localhost:15101
IBKR_BRIDGE_API_TOKEN=<matching bridge's API_TOKEN>
```

Tests skip (not fail) when `LISTENER_ENABLED` is unset, bridge credentials are missing, or the bridge is unreachable. The fill test requires US market hours — it places a MKT order and `pytest.skip()`s if no fill arrives within 10 seconds.

### Local production stack

Run the full production stack on your local machine — no TLS, no Caddy, direct port access:

```bash
make local-up     # build and start all services
make local-down   # stop and remove containers
```

Endpoints after startup:

| Service | URL                                                                            |
| ------- | ------------------------------------------------------------------------------ |
| Relays  | http://localhost:15001/health                                                  |
| Debug   | http://localhost:15003/debug/webhook/{path} (when `DEBUG_WEBHOOK_PATH` is set) |

#### Updating the local stack after code changes

`docker-compose.local.yml` adds read-only bind mounts that shadow the baked-in image files with your local source tree. This means **code changes are visible on container restart — no rebuild needed**:

```bash
make sync                    # restart all containers (when DEFAULT_CLI_RELAY_ENV=local)
make sync ENV=local          # explicit: restart local stack
```

`make local-up` is only needed for the initial build or after changing `requirements.txt` / Dockerfile.

## IBKR Fixtures

Sanitized Flex XML responses live in `services/relays/ibkr/fixtures/`:

- `activity_flex_sample.xml` — Activity Flex (`<Trade>` rows)
- `trade_confirm_sample.xml` — Trade Confirmation (`<TradeConfirm>` rows)

The `TestLiveFixtures` tests in `services/relays/ibkr/test_flex_parser.py` parse these at CI time, so they double as a **schema-drift alarm**: if IBKR renames or removes an attribute the parser depends on, the next fixture refresh fails these tests instead of silently shipping a regression.

### Refreshing a fixture

Requires `IBKR_FLEX_TOKEN` and `IBKR_FLEX_QUERY_ID` (or `_2` suffix variants) in `.env.relays`.

```bash
make ibkr-flex-refresh          # primary query (IBKR_FLEX_QUERY_ID)
make ibkr-flex-refresh S=_2     # secondary query (IBKR_FLEX_QUERY_ID_2)
```

The target:

1. Fetches a live response into `services/relays/ibkr/fixtures/raw.xml`
2. Detects the response type by grepping for `<TradeConfirm` in the XML
3. Runs `sanitize.py` to produce either `activity_flex_sample.xml` or `trade_confirm_sample.xml`
4. Deletes `raw.xml`

If fetch or sanitize fails, `raw.xml` is left in place for inspection — the `&&` chaining in the recipe prevents partial cleanup.

### Just dumping (no sanitize)

```bash
make ibkr-flex-dump F=/tmp/raw.xml       # primary query → file
make ibkr-flex-dump S=_2 F=/tmp/raw2.xml # secondary query → file
make ibkr-flex-dump                      # writes to services/relays/ibkr/fixtures/raw.xml
```

Useful for inspecting a response without overwriting the committed fixture.

### Sanitizer rules

`services/relays/ibkr/fixtures/sanitize.py` is regex-based on `attr="value"` pairs, so it preserves the source document's attribute order and whitespace byte-for-byte apart from the redacted values — ideal for reviewing diffs on refresh. Two classes of replacement:

- **Static attrs** (`accountId`, `acctAlias`, `model`, `traderID`, origin/related IDs) — single constant across every row (account-level facts don't vary per fill).
- **Per-row attrs** (`tradeID`, `ibExecID`/`execID`, `ibOrderID`/`orderID`, `transactionID`, `brokerageOrderID`, `exchOrderId`, `extExecID`) — a 1-indexed counter substituted into a template. First row gets `{n}=1`, second `{n}=2`, etc. Without this the parser's execId-based dedup would collapse multi-row dumps into a single fill.

The sanitizer caps each fixture at `_MAX_ROWS = 3`. Live Flex responses can contain dozens of rows; a fixture only needs a handful for schema-drift detection.

**Idempotent** — re-running `sanitize.py` on an already-sanitized file produces byte-identical output. Safe to run `make ibkr-flex-refresh` repeatedly.

### Safety: raw dumps are gitignored

`.gitignore` ignores `services/relays/ibkr/fixtures/raw*.xml` — raw responses contain real execution IDs (paper or live) and must never be committed. Stick to the `raw*.xml` pattern when dumping manually, or use `make ibkr-flex-refresh` which cleans up after itself.

### When to refresh

- **After noticing unknown attributes in logs.** The parser forwards unknown attrs into `Fill.raw` silently; a fixture refresh is how you'd notice IBKR added new ones.
- **Quarterly.** IBKR adds attributes occasionally. A scheduled refresh catches drift before a real edge case does.
- **After changing the parser.** Edits to `flex_parser.py`'s alias map or `_FILL_TAGS` — re-running the fixture tests verifies nothing regressed.

The committed fixture diff on refresh is itself a useful "what changed at IBKR" log — a refresh with no diff means the schema is stable.

## TypeScript Types

Webhook payload types are available as a TypeScript package under `types/typescript/`:

```
types/typescript/
  index.d.ts                 # Barrel: exports BrokerRelay, RelayApi namespaces
  package.json               # @tradegist/broker-relay-types
  shared/
    index.d.ts               # Re-exports: BuySell, Fill, Trade
    types.d.ts               # Generated from services/shared/models.py (via schema_gen.py)
    types.schema.json         # Intermediate JSON Schema
  relay_api/
    index.d.ts               # Re-exports: WebhookPayloadTrades, WebhookPayload, RunPollResponse, HealthResponse
    types.d.ts               # Generated from services/relay_core/relay_models.py (via schema_gen.py)
    types.schema.json         # Intermediate JSON Schema
```

Usage:

```typescript
import { BrokerRelay, RelayApi } from "@tradegist/broker-relay-types";

const payload: RelayApi.WebhookPayload = ...;    // discriminated union (use this for consumers)
const fill: BrokerRelay.Fill = ...;              // CommonFill primitive
const poll: RelayApi.RunPollResponse = ...;      // relay API types
```

Types are auto-generated from the Pydantic models via `make types`. The `Trade` type follows the CommonFill contract (`orderId`, `symbol`, `side`, `volume`, `price`, `fee`, `cost`, `orderType`, `timestamp`, `source`, `raw`, `fillCount`, `execIds`). The package is not yet published to npm — the API is still evolving.

## Python Types

Pydantic models are also available as a standalone Python package under `types/python/`:

```
types/python/
  pyproject.toml              # b-relay-types, deps: pydantic
  b_relay_types/
    __init__.py               # Re-exports all public types
    shared.py                 # CommonFill primitives (generated from services/shared/models.py)
    relay_api.py              # Relay API types (generated from services/relay_core/relay_models.py)
    notifier/
      __init__.py
      models.py               # Payload contracts (generated from relay_core/notifier/models.py)
```

Usage:

```python
from b_relay_types import Fill, Trade, BuySell                      # CommonFill primitives
from b_relay_types import WebhookPayload, WebhookPayloadTrades       # notifier contracts
from b_relay_types.notifier.models import WebhookPayloadTrades       # direct path
```

Auto-generated by `gen_python_types.py` — each source file is copied verbatim with one import-depth rewrite. Run `make types` to regenerate. Do not edit the generated files manually.

## Project Structure

```
├── Makefile                       # CLI shortcuts (make deploy, make sync, etc.)
├── cli/                           # Python CLI (operator scripts, stdlib only)
│   ├── __init__.py                # Shared helpers (env loading, SSH, DO API, validation)
│   ├── __main__.py                # Entry point (python3 -m cli <command>)
│   ├── poll.py                    # Trigger an immediate poll (relay + index)
│   ├── test_webhook.py            # Send sample trades to webhook endpoint
│   └── core/                      # Project-agnostic (reusable across projects)
│       ├── __init__.py            # CoreConfig, load_env() — loads .env.droplet + .env + .env.relays
│       ├── deploy.py              # Standalone (Terraform + rsync) or shared (rsync + compose)
│       ├── destroy.py             # Terraform destroy
│       ├── pause.py               # Snapshot + delete droplet
│       ├── resume.py              # Restore from snapshot
│       └── sync.py                # rsync files + push .env + .env.relays + restart containers
├── env_examples/                  # Env var templates (make setup copies to .<name>)
│   ├── env                        # App config → .env
│   ├── env.droplet                # CLI-only → .env.droplet
│   ├── env.relays                 # Relay vars → .env.relays
│   └── env.test                   # E2E tests → .env.test
├── docker-compose.yml             # Container orchestration (3 services)
├── docker-compose.shared.yml      # Shared-mode overlay (disables Caddy, uses relay-net)
├── docker-compose.local.yml       # Local dev override (direct port access, no TLS)
├── docker-compose.test.yml        # Test stack override (env_file: !override)
├── terraform/
│   ├── main.tf                    # Droplet, firewall, reserved IP, SSH key
│   ├── variables.tf               # Terraform variables
│   ├── outputs.tf                 # Droplet IP, Site URL, SSH key
│   └── cloud-init.sh              # Docker install + creates project directory
├── services/
│   ├── relay_core/                # Main container: registry + engines + HTTP API
│   │   ├── __init__.py            # BrokerRelay dataclass, re-exports engine types
│   │   ├── main.py                # Entrypoint (loads relays, starts pollers + listeners + API)
│   │   ├── registry.py            # Relay registry (RELAYS env var → adapter loading)
│   │   ├── poller_engine.py       # Generic poller (dedup, fetch, parse, notify, mark)
│   │   ├── listener_engine.py     # Generic WS listener (connect, dedup, notify, reconnect)
│   │   ├── relay_models.py        # Re-export shim (shared models + RunPollResponse, HealthResponse)
│   │   ├── dedup/                 # SQLite dedup library
│   │   │   └── __init__.py        # init_db(), is_processed(), mark_processed(), prune()
│   │   ├── notifier/              # Pluggable notification backends
│   │   │   ├── __init__.py        # Registry, load_notifiers(), validate_notifier_env(), notify()
│   │   │   ├── base.py            # BaseNotifier ABC
│   │   │   └── webhook.py         # WebhookNotifier: HMAC-SHA256 signed HTTP POST
│   │   ├── routes/                # HTTP API
│   │   │   ├── __init__.py        # create_app(), start_api_server(), handle_health, handle_poll
│   │   │   └── middlewares.py     # Auth middleware (Bearer token, AUTH_PREFIX=/relays)
│   │   ├── tests/e2e/             # E2E tests (smoke + listener)
│   │   │   └── conftest.py        # httpx fixtures + two-tier preflight
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── relays/                    # Broker adapters (one package per broker)
│   │   ├── ibkr/                  # IBKR adapter
│   │   │   ├── __init__.py        # build_relay(), env getters, map_fill()
│   │   │   ├── bridge_models.py   # Mirrored WsEnvelope types from ibkr_bridge
│   │   │   ├── flex_fetch.py      # Flex Web Service two-step fetch
│   │   │   └── flex_parser.py     # Flex XML parser (Activity + Trade Confirmation)
│   │   └── kraken/                # Kraken crypto exchange adapter
│   │       ├── __init__.py        # build_relay(), env getters, REST + WS adapters
│   │       ├── rest_client.py     # KrakenClient: HMAC-SHA512 auth, trades, WS token
│   │       ├── ws_parser.py       # WS v2 executions parser
│   │       └── kraken_types.py    # TypedDicts for raw Kraken API shapes
│   ├── shared/                    # Shared models and utilities (library, no container)
│   │   ├── __init__.py            # Barrel: re-exports models + utilities
│   │   ├── models.py              # Pydantic models (Fill, Trade, WebhookPayload, BuySell, RelayName)
│   │   └── utilities.py           # Internal helpers (aggregate_fills, normalize_*, _dedup_id)
│   └── debug/                     # Debug webhook inbox service
│       ├── debug_app.py           # aiohttp app: POST/GET/DELETE /debug/webhook/{path}
│       ├── Dockerfile
│       └── requirements.txt
├── infra/
│   └── caddy/
│       ├── Caddyfile              # Reverse proxy config (SITE_DOMAIN)
│       └── sites/
│           ├── ibkr.caddy         # /relays/* routes → relays:8000
│           └── debug.caddy        # /debug/webhook/* → debug:9000
├── types/
│   ├── typescript/                # @tradegist/broker-relay-types (BrokerRelay + RelayApi namespaces)
│   │   ├── index.d.ts             # Barrel: exports BrokerRelay, RelayApi
│   │   ├── package.json
│   │   ├── shared/                # BrokerRelay namespace (CommonFill primitives)
│   │   │   ├── index.d.ts
│   │   │   └── types.d.ts         # Generated from services/shared/models.py
│   │   └── relay_api/             # RelayApi namespace (payload contracts + API types)
│   │       ├── index.d.ts
│   │       └── types.d.ts         # Generated from services/relay_core/relay_models.py
│   └── python/                    # b-relay-types PyPI package
│       ├── pyproject.toml
│       └── b_relay_types/
│           ├── __init__.py        # Re-exports all public types
│           ├── shared.py          # Generated from services/shared/models.py
│           ├── relay_api.py       # Generated from services/relay_core/relay_models.py
│           └── notifier/
│               └── models.py      # Generated from relay_core/notifier/models.py
├── schema_gen.py                  # JSON Schema generator (Pydantic → TS types)
└── gen_python_types.py            # Python types generator (models → types/python/)
```

## Flex XML Parsing

The relay supports both **Activity Flex Queries** (`<Trade>` tags) and **Trade Confirmation Flex Queries** (`<TradeConfirm>` / `<TradeConfirmation>` tags). To handle both formats in a unified way, the parser makes the following assumptions:

- **Field names are normalized** to a single canonical name when IBKR uses different attribute names across formats:

  | Canonical name       | Activity Flex attribute | Trade Confirmation attribute |
  | -------------------- | ----------------------- | ---------------------------- |
  | `price`              | `tradePrice`            | `price`                      |
  | `commission`         | `ibCommission`          | `commission`                 |
  | `commissionCurrency` | `ibCommissionCurrency`  | `commissionCurrency`         |
  | `orderId`            | `ibOrderID`             | `orderID`                    |
  | `transactionId`      | `transactionID`         | —                            |
  | `ibExecId`           | `ibExecID`              | `execID`                     |
  | `taxes`              | `taxes`                 | `tax`                        |
  | `settleDateTarget`   | `settleDateTarget`      | `settleDate`                 |
  | `tradeMoney`         | `tradeMoney`            | `amount`                     |

- **All known fields are preserved in `raw`** — the full IBKR XML attributes are captured in the `raw` dict on each Fill (and propagated to Trade). CommonFill fields (`execId`, `symbol`, `side`, `volume`, `price`, `fee`, `cost`, `timestamp`, `orderType`, `source`) are extracted as top-level fields. Unknown XML attributes also appear in `raw` but are not reported as errors.

- **Fills are aggregated into trades** by `orderId`. When an order has multiple fills:
  - `volume` is the sum of all fills
  - `price` is the quantity-weighted average (VWAP)
  - Financial fields (`cost`, `fee`) are summed
  - `timestamp` uses the latest value across fills
  - `raw` comes from the first fill
  - `execIds` is an array of execution IDs (one per fill), so you can trace back to individual executions
  - `fillCount` is the number of fills in the group

- **Deduplication** uses `execId` as the primary key. For Flex XML, `execId` is resolved via a fallback chain: `ibExecId` → `transactionId` → `tradeID`. `ibExecId` is preferred because it is the most specific identifier. Processed IDs are stored in a SQLite database (WAL mode).

- **Parse errors never break the runtime.** Malformed rows are skipped and reported in the `errors` array. Bad float values default to `0.0`.

The XML parsing logic lives in [`services/relays/ibkr/flex_parser.py`](services/relays/ibkr/flex_parser.py).

If you notice any mistakes in the webhook payload or field mapping, please [open a PR](../../pulls).

## IBKR ID Reference

IBKR uses different field names for the same identifiers across its APIs. This table maps them:

| Concept                | TWS / ib_async | Flex Activity (AF) | Flex Trade Confirm (TC) | Notes                                                                                                                            |
| ---------------------- | -------------- | ------------------ | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| **Permanent order ID** | `permId`       | `ibOrderID`        | `orderID`               | Account-wide, survives reconnects. The only reliable cross-session order identifier. Exposed as `orderId` in this project's API. |
| Session order ID       | `orderId`      | —                  | —                       | Client-scoped `int`, resets on reconnect. Not used in this project.                                                              |
| Execution / fill ID    | `execId`       | `ibExecID`         | `execID`                | Per-fill unique ID. Format: `hex.hex.seq.seq`. Join key between real-time and Flex at the fill level.                            |
| Transaction ID         | —              | `transactionId`    | —                       | Flex-only monotonic ID. Fallback dedup key when `ibExecId` is absent.                                                            |
| Trade ID               | —              | `tradeID`          | —                       | Flex reporting grouping key. No real-time equivalent.                                                                            |
| Brokerage order ID     | —              | `brokerageOrderID` | —                       | IBKR internal routing ID.                                                                                                        |
| Exchange order ID      | —              | `exchOrderId`      | —                       | ID assigned by the exchange.                                                                                                     |
| External exec ID       | —              | `extExecID`        | —                       | Execution ID from the exchange.                                                                                                  |

**Cross-API join keys:**

- **Order level:** `permId` (TWS) ↔ `ibOrderID` (Flex AF) ↔ `orderID` (Flex TC)
- **Fill level:** `execId` (TWS) ↔ `ibExecID` (Flex AF) ↔ `execID` (Flex TC)

**This project's convention:** The permanent order ID is exposed as `orderId` in `Trade` objects. The execution/fill ID is exposed as `execId` on `Fill` and in the `execIds` array on `Trade`. The dedup key is `execId`, resolved via the fallback chain `ibExecId` → `transactionId` → `tradeID` at parse time.
