# IBKR Webhook Relay

Poll the IBKR [Flex Web Service](https://www.interactivebrokers.com/campus/ibkr-api-page/flex-web-service/) for trade fills and forward them to your webhook URL — deployed to a DigitalOcean droplet with a single `make deploy`.

> [!WARNING]
> This project is under active development and not yet ready for prime time. You're welcome to use it, but expect frequent breaking changes.

## Why This Project?

IBKR's Flex Web Service is the most reliable way to get trade confirmations — it works without an active Gateway session, so you can trade normally via web or mobile and know that fills will be captured. But building the polling infrastructure (scheduled fetches, XML parsing, dedup, webhook delivery, HTTPS) takes time.

This project bundles everything into a single `make deploy` that provisions a DigitalOcean droplet (starting at **$4/month**) with:

- **A poller** that checks for trade fills every 10 minutes and sends them to your webhook URL
- **Automatic HTTPS** via Caddy + Let's Encrypt
- **SQLite dedup** so each fill is delivered exactly once
- **A debug webhook inbox** for testing without hitting production services
- **An optional second poller** for a different IBKR account/query
- **An optional real-time listener** that subscribes to [ibkr_bridge](https://github.com/tradegist/ibkr_bridge)'s WebSocket stream for instant fill delivery

> **Looking for order placement?** See [ibkr_bridge](https://github.com/tradegist/ibkr_bridge) — a companion project that runs the IB Gateway and exposes an HTTP API for placing orders and a real-time WebSocket event stream.

## Table of Contents

- [API Endpoints](#api-endpoints)
- [Architecture](#architecture)
- [Quick Start](#quick-start-local-deploy)
- [Configuration](#configuration)
- [Webhook Payload](#webhook-payload)
  - [Debug Webhook Inbox](#debug-webhook-inbox)
- [Flex Web Service Setup](#flex-web-service-setup)
- [On-Demand Poll](#on-demand-poll)
- [Real-Time Listener](#real-time-listener)
- [Commands](#commands)
- [Pause & Resume](#pause--resume)
- [Security](#security)
- [Testing](#testing)
- [TypeScript Types](#typescript-types)
- [Project Structure](#project-structure)
- [Current Status](#current-status)
- [Flex XML Parsing](#flex-xml-parsing)
- [IBKR ID Reference](#ibkr-id-reference)

## API Endpoints

All endpoints require `Authorization: Bearer <API_TOKEN>` header (except health).

#### Trigger a poll

```
POST /ibkr/poller/run
```

No body required. Immediately polls the Flex Web Service for new fills and sends them to the configured webhook.

#### Health check

```
GET /health
```

Returns `{"status": "ok"}`. No auth required.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  DigitalOcean Droplet                                    │
│                                                          │
│  ┌──────────────────────────────────────────────┐        │
│  │  caddy (reverse proxy + auto HTTPS)          │        │
│  │  trade.example.com → poller:8000             │        │
│  │  Ports: 80 (HTTP→redirect), 443 (HTTPS)      │        │
│  └──────────────┬──────────────────┬────────────┘        │
│                 │                  │                      │
│  ┌──────────────▼───────┐  ┌──────▼──────────────────┐   │
│  │  poller              │  │  ibkr-debug (optional)   │   │
│  │  Flex Web Service    │  │  Webhook payload inbox   │   │
│  │  → Webhook POST      │  │  POST/GET/DELETE         │   │
│  │  SQLite dedup        │  └─────────────────────────┘   │
│  │  + Listener (WS)     │                                │
│  └──────────────────────┘                                │
│                                                          │
│  [poller-2] (optional, same image, different account)    │
│                                                          │
│  Firewall: SSH from deployer IP only                     │
│  HTTP/HTTPS open (Caddy auto-redirects HTTP → HTTPS)     │
└──────────────────────────────────────────────────────────┘
```

Four containers in a single Docker network (poller-2 and ibkr-debug are optional):

- **`caddy`** — [Caddy 2](https://caddyserver.com/) reverse proxy with automatic HTTPS via Let's Encrypt. Routes `/ibkr/*` to the poller.
- **`poller`** — Python image that polls the IBKR Flex Web Service every 10 minutes for new fills and sends them via pluggable notification backends (see `services/notifier/`). Supports both **Trade Confirmation** and **Activity** Flex Query types. Uses SQLite for deduplication. Also hosts an optional **real-time listener** that subscribes to [ibkr_bridge](https://github.com/tradegist/ibkr_bridge)'s WebSocket event stream for instant fill delivery. **Does not hold an IBKR session** — trade normally via web/mobile.
- **`poller-2`** — Optional second poller instance (behind the `poller2` profile) for a different IBKR account or query. Same image, separate config via `_2` suffixed env vars.
- **`ibkr-debug`** — Optional debug webhook inbox. Captures webhook payloads for inspection during development. Enabled when `DEBUG_WEBHOOK_PATH` is set.

> **Dedup guarantee.** The poller uses a SQLite dedup database so each fill is delivered at most once under normal operation. In the rare event of an internal crash between webhook delivery and dedup bookkeeping, a fill may be sent a second time. Design your webhook consumer to be idempotent (e.g. deduplicate on `execId`).

## Domains & HTTPS

A domain name is **required**. Caddy uses it to automatically provision a TLS certificate from Let's Encrypt. Without a valid domain, Caddy cannot obtain a certificate and the service will not be accessible — there is no fallback to plain HTTP or IP-based access.

### Setup

1. Point the domain to the droplet's reserved IP as an **A record**:
   ```
   trade.example.com  A  1.2.3.4
   ```
2. Set it in `.env`:
   ```
   SITE_DOMAIN=trade.example.com
   ```
3. Start the stack — Caddy will automatically obtain and renew the certificate.

> **Can I use just an IP address?** No. Let's Encrypt does not issue certificates for bare IP addresses.

## Droplet Sizing

Set `DROPLET_SIZE` in `.env` to control the droplet size. The poller is lightweight — the smallest droplet ($4/month) works fine:

```env
DROPLET_SIZE=s-1vcpu-512mb   # $4/month (default)
```

## Quick Start (Local Deploy)

### Prerequisites

- [Docker Compose v2](https://docs.docker.com/compose/install/) (the Go rewrite, `docker compose` — not the legacy Python `docker-compose`). Required for `deploy.replicas` support, which is how `POLLER_ENABLED` disables services.
- [Terraform](https://developer.hashicorp.com/terraform/install) installed
- A [DigitalOcean API token](https://cloud.digitalocean.com/account/api/tokens)
- An IBKR account with Flex Web Service enabled (see [Flex Web Service Setup](#flex-web-service-setup))

### Steps

```bash
# 1. Clone and configure
git clone https://github.com/OWNER/ibkr_relay.git
cd ibkr_relay
make setup        # Create .venv and install dependencies
cp .env.example .env
# Edit .env with your values

# 2. Deploy
make deploy

# 3. Tear down when done
make destroy
```

## Testing

```bash
make test        # run pytest (poller, parser, models)
make typecheck   # strict mypy checking
make lint        # run ruff linter
```

### E2E tests

E2E tests run against a local Docker stack (`docker-compose.test.yml`) with the poller and debug webhook inbox.

```bash
make e2e          # start stack → run tests → stop stack
make e2e-up       # start test stack (idempotent)
make e2e-run      # run E2E tests (stack must be up)
make e2e-down     # stop and remove test stack
```

- Credentials live in `.env.test` (gitignored). Template: `.env.test.example`.
- `make e2e-run` restarts `poller` and `ibkr-debug` containers to pick up code changes from volume mounts, then runs the E2E tests. Safe to call repeatedly — no rebuild needed.
- Test poller runs on `localhost:15011` with token `test-token`.

#### Listener E2E tests

Listener E2E tests are **opt-in** — they require a running [ibkr_bridge](https://github.com/tradegist/ibkr_bridge) local stack and additional `.env.test` variables:

```env
LISTENER_ENABLED=true
BRIDGE_WS_URL=ws://host.docker.internal:15101/ibkr/ws/events
BRIDGE_API_BASE_URL=http://localhost:15101
BRIDGE_API_TOKEN=<matching bridge's API_TOKEN>
```

Tests skip (not fail) when `LISTENER_ENABLED` is unset, bridge credentials are missing, or the bridge is unreachable. The fill test requires US market hours — it places a MKT order and `pytest.skip()`s if no fill arrives within 10 seconds.

### Local production stack

Run the full production stack on your local machine — no TLS, no Caddy, direct port access:

```bash
make local-up     # build and start all services
make local-down   # stop and remove containers
```

`make local-up` reads `.env` and honors `POLLER_ENABLED`.

Endpoints after startup:

| Service  | URL                                                                            |
| -------- | ------------------------------------------------------------------------------ |
| Poller   | http://localhost:15001/health                                                  |
| Poller-2 | http://localhost:15002/health (when `IBKR_FLEX_QUERY_ID_2` is set)             |
| Debug    | http://localhost:15003/debug/webhook/{path} (when `DEBUG_WEBHOOK_PATH` is set) |

#### Updating the local stack after code changes

`docker-compose.local.yml` adds read-only bind mounts that shadow the baked-in image files with your local source tree. This means **code changes are visible on container restart — no rebuild needed**:

```bash
make sync                    # restart all containers (when DEFAULT_CLI_RELAY_ENV=local)
make sync ENV=local          # explicit: restart local stack
```

`make local-up` is only needed for the initial build or after changing `requirements.txt` / Dockerfile.

## TypeScript Types

Webhook payload types are available as a TypeScript package under `types/typescript/`:

```
types/typescript/
  index.d.ts                 # Barrel: exports Ibkr, IbkrPoller namespaces
  package.json               # @tradegist/ibkr-relay-types
  shared/
    index.d.ts               # Re-exports: BuySell, Fill, Trade, WebhookPayloadTrades, WebhookPayload
    types.d.ts               # Generated from services/shared/models.py (via schema_gen.py)
    types.schema.json         # Intermediate JSON Schema
  poller/
    index.d.ts               # Re-exports: RunPollResponse, HealthResponse
    types.d.ts               # Generated from services/poller/poller_models.py (via schema_gen.py)
    types.schema.json         # Intermediate JSON Schema
```

Usage:

```typescript
import { Ibkr, IbkrPoller } from "@tradegist/ibkr-relay-types";

const payload: Ibkr.WebhookPayload = ...;   // discriminated union (use this for consumers)
const poll: IbkrPoller.RunPollResponse = ...; // poller-specific types
```

Types are auto-generated from the Pydantic models via `make types`. The `Trade` type follows the CommonFill contract (`orderId`, `symbol`, `side`, `volume`, `price`, `fee`, `cost`, `orderType`, `timestamp`, `source`, `raw`, `fillCount`, `execIds`). The package is not yet published to npm — the API is still evolving.

## Python Types

Pydantic models are also available as a standalone Python package under `types/python/`:

```
types/python/
  pyproject.toml              # ibkr-relay-types, deps: pydantic
  ibkr_relay_types/
    __init__.py               # Re-exports all public types
    shared.py                 # CommonFill models (generated from services/shared/models.py)
    poller.py                 # Poller API types (generated from poller_models.py)
```

Usage:

```python
from ibkr_relay_types import Fill, Trade, WebhookPayload, BuySell
```

Auto-generated by `gen_python_types.py` — run `make types` to regenerate. Do not edit the generated files manually.

## Configuration

All configuration is via environment variables in `.env`:

| Variable                       | Required | Default             | Description                                                                                                     |
| ------------------------------ | -------- | ------------------- | --------------------------------------------------------------------------------------------------------------- |
| `DEPLOY_MODE`                  | Yes      | —                   | `standalone` (own droplet via Terraform) or `shared` (deploy to existing droplet)                               |
| `DO_API_TOKEN`                 | Yes\*    | —                   | DigitalOcean API token (standalone mode only — can be removed after first deploy)                               |
| `DROPLET_IP`                   | Yes\*    | —                   | Droplet IP (from Terraform output in standalone; provided by host in shared)                                    |
| `SSH_KEY`                      | No       | `~/.ssh/ibkr-relay` | SSH key path — **shared mode only**. In standalone, Terraform auto-generates and saves the key; never set this. |
| `SITE_DOMAIN`                  | Yes      | —                   | Domain for the poller API (see [Domains & HTTPS](#domains--https))                                              |
| `API_TOKEN`                    | Yes      | —                   | Bearer token for `/ibkr/*` endpoints (`openssl rand -hex 32`)                                                   |
| `IBKR_FLEX_TOKEN`              | Yes      | —                   | Flex Web Service token (from Client Portal)                                                                     |
| `IBKR_FLEX_QUERY_ID`           | Yes      | —                   | Flex Query ID (Trade Confirmation or Activity)                                                                  |
| `TARGET_WEBHOOK_URL`           | No       | —                   | Webhook endpoint (empty = log-only dry-run)                                                                     |
| `WEBHOOK_SECRET`               | No       | —                   | HMAC-SHA256 key for signing payloads (required if NOTIFIERS=webhook)                                            |
| `NOTIFIERS`                    | No       | —                   | Active notification backends (e.g. `webhook`). Empty = dry-run                                                  |
| `POLLER_ENABLED`               | No       | `true`              | Set to `false` to disable the poller container entirely                                                         |
| `DROPLET_SIZE`                 | No       | `s-1vcpu-512mb`     | Override droplet size slug (e.g. `s-1vcpu-1gb`)                                                                 |
| `POLL_INTERVAL_SECONDS`        | No       | `600`               | Flex poll interval (seconds)                                                                                    |
| `LISTENER_ENABLED`             | No       | —                   | Set to `true` to enable real-time WS listener (requires ibkr_bridge)                                            |
| `BRIDGE_WS_URL`                | No\*     | —                   | ibkr_bridge WebSocket URL (required when listener enabled)                                                      |
| `BRIDGE_API_TOKEN`             | No\*     | —                   | Bearer token for bridge WS auth (must match bridge's `API_TOKEN`)                                               |
| `LISTENER_EXEC_EVENTS_ENABLED` | No       | `false`             | Enable `execDetailsEvent` webhooks (2x volume, lower latency)                                                   |
| `LISTENER_EVENT_DEBOUNCE_TIME` | No       | `0`                 | Milliseconds to buffer fills before flushing                                                                    |
| `DEBUG_WEBHOOK_PATH`           | No       | —                   | Route webhooks to debug inbox instead of `TARGET_WEBHOOK_URL` (see [Debug Webhook Inbox](#debug-webhook-inbox)) |
| `MAX_DEBUG_WEBHOOK_PAYLOADS`   | No       | `100`               | Max payloads stored in the debug inbox (hard max: 150, FIFO eviction)                                           |
| `DEBUG_LOG_LEVEL`              | No       | `INFO`              | Set to `DEBUG` to include full payload+headers in `docker logs ibkr-debug`                                      |

**Second poller (optional):** Set `IBKR_FLEX_QUERY_ID_2` to enable a second independent poller with its own webhook destination. `IBKR_FLEX_TOKEN_2` is optional — when omitted, the primary `IBKR_FLEX_TOKEN` is used (useful when both queries share the same token). All `_2` suffixed env vars (`NOTIFIERS_2`, `TARGET_WEBHOOK_URL_2`, `WEBHOOK_SECRET_2`, `POLL_INTERVAL_SECONDS_2`, etc.) follow the same pattern.
| `TIME_ZONE` | No | `America/New_York` | Timezone (tz database format) |

## Webhook Payload

When orders fill, the relay POSTs a JSON payload with all trades batched into a single request:

```json
{
  "relay": "ibkr",
  "type": "trades",
  "data": [
    {
      "orderId": "684196618",
      "symbol": "AAPL",
      "assetClass": "equity",
      "side": "buy",
      "orderType": "market",
      "price": 254.6,
      "volume": 1.0,
      "cost": 254.6,
      "fee": -1.0,
      "fillCount": 1,
      "execIds": ["0001f4e8.67890abc.01.01"],
      "timestamp": "20260402;093008",
      "source": "flex",
      "raw": {
        "accountId": "UXXXXXXX",
        "assetCategory": "STK",
        "currency": "USD",
        "commission": -1.0,
        "commissionCurrency": "USD",
        "tradeDate": "20260402",
        "dateTime": "20260402;093008",
        "orderTime": "20260401;183713",
        "orderType": "MKT",
        "listingExchange": "NASDAQ",
        "exchange": "IBDARK",
        "underlyingSymbol": "AAPL"
      }
    }
  ],
  "errors": []
}
```

The envelope uses a discriminated union pattern — `relay` identifies the exchange and `type` identifies the event kind. Consumers should type their variables as `WebhookPayload` (the union). Currently the only variant is `WebhookPayloadTrades` (`type: "trades"`); new event types (e.g. orders, positions) will be added as new variants.

### CommonFill Contract

All exchange relays (IBKR, Kraken, etc.) use the same **CommonFill** model. The `data` array contains `Trade` objects with these guaranteed fields:

| Field        | Type                | Description                                                                               |
| ------------ | ------------------- | ----------------------------------------------------------------------------------------- |
| `orderId`    | `string`            | Permanent order identifier (unique per account)                                           |
| `symbol`     | `string`            | Instrument symbol                                                                         |
| `assetClass` | `AssetClass`        | `"equity"`, `"option"`, `"crypto"`, `"future"`, `"forex"`, or `"other"`                   |
| `side`       | `"buy" \| "sell"`   | Trade direction (lowercase)                                                               |
| `orderType`  | `OrderType \| null` | Normalized: `"market"`, `"limit"`, `"stop"`, `"stop_limit"`, `"trailing_stop"`, or `null` |
| `price`      | `number`            | VWAP when aggregated, single fill price otherwise                                         |
| `volume`     | `number`            | Sum of fill quantities                                                                    |
| `cost`       | `number`            | Total cost (sum of fills)                                                                 |
| `fee`        | `number`            | Total fees/commissions (sum of fills)                                                     |
| `fillCount`  | `number`            | Number of fills aggregated into this trade                                                |
| `execIds`    | `string[]`          | One execution ID per fill (for tracing back to individual fills)                          |
| `timestamp`  | `string`            | Latest fill timestamp                                                                     |
| `source`     | `string`            | Origin: `"flex"` (from Flex Web Service poll)                                             |
| `raw`        | `object`            | Original exchange-specific payload (all fields, unmodified)                               |

The `raw` object preserves the full exchange-specific data. For IBKR Flex, this includes ~100 XML attributes (account info, security details, financial fields, dates). Consumers should treat `raw` as opaque exchange data — the CommonFill fields above are the stable contract.

The `errors` array contains warnings about parse problems — it is empty when everything parsed cleanly.

The payload is signed with HMAC-SHA256. Verify using the `X-Signature-256` header:

```python
# Python
import hashlib, hmac

expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
assert header_value == f"sha256={expected}"
```

```js
// Node.js
const crypto = require("crypto");

const expected = crypto.createHmac("sha256", secret).update(body).digest("hex");
assert(headerValue === `sha256=${expected}`);
```

If `TARGET_WEBHOOK_URL` is empty, the relay logs the payload to stdout (dry-run mode) instead of sending it.

### Debug Webhook Inbox

To test webhook delivery without hitting production services (e.g. Pipedream), set `DEBUG_WEBHOOK_PATH` in `.env`:

```env
DEBUG_WEBHOOK_PATH=abcdef
#MAX_DEBUG_WEBHOOK_PAYLOADS=100
#DEBUG_LOG_LEVEL=INFO
```

This starts the `ibkr-debug` container and reroutes all webhook delivery to an in-memory inbox at `/debug/webhook/<path>`. The real `TARGET_WEBHOOK_URL` is ignored while this is set.

**Inspect captured payloads:**

```bash
# View all stored payloads
curl -s https://trade.example.com/debug/webhook/abcdef | python3 -m json.tool

# Clear the inbox
curl -s -X DELETE https://trade.example.com/debug/webhook/abcdef
```

**Stream payloads in real time** — set `DEBUG_LOG_LEVEL=DEBUG` and tail the container logs:

```bash
make logs S=ibkr-debug
# or: docker logs -f ibkr-debug
```

Payloads are logged at DEBUG level (full payload + headers). At INFO level (default), only a count summary is logged. Log rotation is aggressive (`max-size: 10k`) so sensitive data does not accumulate on disk.

To disable, remove or comment out `DEBUG_WEBHOOK_PATH` and run `make sync`. The container stops automatically (`DEBUG_REPLICAS=0`).

## Commands

All operations are available via `make` or the Python CLI directly. Run `make help` to see the full list:

```
  make deploy      Deploy infrastructure (Terraform + Docker)
  make destroy     Permanently destroy all infrastructure
  make pause       Snapshot droplet + delete (save costs)
  make resume      Restore droplet from snapshot
  make setup       Create .venv and install all dependencies
  make sync        Push .env + restart (S=service B=1 LOCAL_FILES=1 ENV=local)
  make poll        Trigger an immediate Flex poll (V=1 verbose, DEBUG=1 XML, REPLAY=N resend)
  make poll2       Trigger immediate Flex poll (second poller)
  make test-webhook Send sample trades to webhook endpoint
  make types       Regenerate TypeScript types from Pydantic models
  make test        Run unit tests (pytest)
  make typecheck   Run mypy strict type checking
  make lint        Run ruff linter (FIX=1 to auto-fix)
  make e2e         Run E2E tests (starts/stops stack automatically)
  make e2e-up      Start E2E test stack (poller + ibkr-debug)
  make e2e-run     Run E2E tests (stack must be up)
  make e2e-down    Stop and remove E2E test stack
  make local-up    Start full stack locally (no TLS, direct port access)
  make local-down  Stop local stack
  make logs        Stream logs (S=service ENV=local, default: poller on droplet)
  make stats       Show container resource usage
  make ssh         SSH into the droplet
  make help        Show available commands
```

You can also invoke the CLI directly with `python3 -m cli <command>` — useful on Windows or when Make is not available:

> [!NOTE]
> Most commands work on Windows, but `sync --local-files` requires `rsync` and SSH, which are only available natively on macOS and Linux. On Windows, use [WSL](https://learn.microsoft.com/en-us/windows/wsl/).

```bash
python3 -m cli deploy
python3 -m cli sync poller
python3 -m cli poll
python3 -m cli poll 2 # second poller
python3 -m cli test-webhook   # send sample trades to webhook 1
python3 -m cli test-webhook 2 # send sample trades to webhook 2
python3 -m cli pause
python3 -m cli resume
python3 -m cli destroy
```

`make` examples:

```bash
make deploy                                    # provision droplet + start containers
make sync                                      # push .env + restart all services
make sync S=poller                             # push .env + restart one service
make sync B=1                                  # push .env + rebuild images + restart
make sync LOCAL_FILES=1                        # rsync files + rebuild + restart (full deploy)
make sync LOCAL_FILES=1 S=poller               # full deploy, rebuild only poller
make poll                                      # trigger immediate Flex poll
make poll V=1                                  # verbose (SSH, full poller logs)
make poll DEBUG=1                              # dump raw Flex XML
make poll REPLAY=3                             # resend 3 trades (skip dedup)
make poll2                                     # trigger second poller
make test-webhook                              # send 3 sample trades to webhook
make test-webhook S=2                          # send to second webhook
make test                                      # run unit tests
make typecheck                                 # strict mypy checking
make logs                                      # stream poller logs (droplet)
make logs S=poller-2                           # stream second poller logs
make logs S=ibkr-debug                         # stream debug inbox logs
make logs ENV=local                            # stream local stack logs
```

```bash
make pause                           # snapshot + delete droplet
make resume                          # restore from snapshot
```

### Which service to sync

After changing a variable in `.env`, restart only the affected service:

| Variable                                                                                                                              | Service | Command              |
| ------------------------------------------------------------------------------------------------------------------------------------- | ------- | -------------------- |
| `API_TOKEN`                                                                                                                           | poller  | `make sync S=poller` |
| `IBKR_FLEX_TOKEN`, `IBKR_FLEX_QUERY_ID`, `TARGET_WEBHOOK_URL`, `WEBHOOK_SECRET`, `WEBHOOK_HEADER_NAME/VALUE`, `POLL_INTERVAL_SECONDS` | poller  | `make sync S=poller` |
| `POLLER_ENABLED`                                                                                                                      | poller  | `make sync`          |
| `SITE_DOMAIN`                                                                                                                         | caddy   | `make sync S=caddy`  |
| Multiple services or unsure                                                                                                           | all     | `make sync`          |

**One-shot overrides** — toggle services for a single command without editing `.env`:

```bash
make sync POLLER=0           # disable poller
make local-up POLLER=0       # start local stack without poller
```

### Syncing code changes

#### Local stack

When `DEFAULT_CLI_RELAY_ENV=local` (or `ENV=local`), `make sync` simply restarts all containers. Bind mounts in `docker-compose.local.yml` ensure your code changes are picked up automatically — no rebuild needed:

```bash
make sync              # restart containers (when DEFAULT_CLI_RELAY_ENV=local)
make sync ENV=local    # explicit override
```

#### Remote droplet

`make sync` only pushes `.env` and restarts containers — it does **not** update source files on the droplet. When you change Python code, Dockerfiles, or Compose config, use `LOCAL_FILES=1` to sync everything:

```bash
make sync LOCAL_FILES=1
```

This runs a full pre-deploy pipeline before anything reaches the droplet:

1. Verify you're on `main` (aborts on feature branches)
2. Verify working tree is clean (aborts on uncommitted changes)
3. `make typecheck` — mypy strict type checking
4. `make test` — all unit tests
5. `rsync` project files to the droplet (respects `.gitignore`, excludes `.env`)
6. Push `.env`
7. `docker compose up -d --build --force-recreate`

If any step fails, the deploy aborts — nothing reaches the droplet.

If you forked this repo, pull upstream changes first, then deploy:

```bash
git pull upstream main   # merge latest changes from upstream
make sync LOCAL_FILES=1  # deploy to your droplet
```

## Project Structure

```

├── Makefile # CLI shortcuts (make deploy, make sync, etc.)
├── cli/ # Python CLI (replaces shell scripts)
│ ├── __init__.py # Project-specific config (CoreConfig setup, helpers)
│ ├── __main__.py # Entry point (python3 -m cli <command>)
│ ├── poll.py # Trigger an immediate Flex poll
│ ├── test_webhook.py # Send sample trades to webhook endpoint
│ └── core/ # Project-agnostic (reusable across projects)
│   ├── __init__.py # CoreConfig dataclass, generic helpers (env, SSH, DO API, Terraform)
│   ├── deploy.py # Standalone (Terraform + rsync) or shared (rsync + compose)
│   ├── destroy.py # Terraform destroy
│   ├── pause.py # Snapshot + delete droplet
│   ├── resume.py # Restore from snapshot
│   └── sync.py # rsync files + pre-deploy checks + restart containers
├── .env.example # Configuration template
├── .github/workflows/
│   └── ci.yml                     # GitHub Actions: lint → typecheck → test
├── terraform/
│ ├── main.tf # Droplet, firewall, reserved IP, SSH key
│ ├── variables.tf # Terraform variables (infrastructure only)
│ ├── outputs.tf # Droplet IP, Site URL, SSH key
│ └── cloud-init.sh # Docker install + creates project directory
├── docker-compose.yml # Container orchestration (4 services)
├── docker-compose.shared.yml # Shared-mode overlay (disables Caddy, uses relay-net)
├── docker-compose.local.yml # Local dev override (direct port access, no TLS)
├── services/                  # Business-logic services (user-facing features)
│   ├── shared/                    # Single source of truth for models + utilities
│   │   └── __init__.py            # Fill, Trade, WebhookPayload, BuySell, OrderType, aggregate_fills()
│   ├── poller/
│   │   ├── Dockerfile
│   │   ├── requirements.txt       # httpx, pydantic, aiohttp
│   │   ├── main.py                # Entrypoint (polling loop + HTTP API)
│   │   ├── poller_models.py       # Re-export shim (shared models + poller-specific API types)
│   │   ├── poller/                # Core polling logic (package)
│   │   │   ├── __init__.py        # SQLite dedup, Flex fetch, poll_once()
│   │   │   ├── flex_parser.py     # Flex XML parser (Activity + Trade Confirmation)
│   │   │   ├── test_flex_parser.py # Tests for flex_parser
│   │   │   └── test_poller.py     # Tests for poller core logic
│   │   └── poller_routes/         # HTTP API
│   │       ├── __init__.py        # Route orchestrator (create_routes, start_api_server)
│   │       ├── health.py          # GET /health handler
│   │       ├── middlewares.py     # Auth middleware (Bearer token)
│   │       ├── run.py             # POST /ibkr/poller/run handler
│   │       └── test_middlewares.py # Tests for auth middleware
│   ├── notifier/                  # Pluggable notification backends (library, no container)
│   │   ├── __init__.py            # Registry, load_notifiers(), validate_notifier_env(), notify()
│   │   ├── base.py                # BaseNotifier ABC
│   │   └── webhook.py             # WebhookNotifier: HMAC-SHA256 signed HTTP POST
│   ├── dedup/                     # Shared SQLite dedup library (library, no container)
│   │   └── __init__.py            # init_db(), is_processed(), mark_processed(), get_processed_ids(), prune()
│   └── debug/                     # Debug webhook inbox service
│       ├── debug_app.py           # aiohttp app: POST/GET/DELETE /debug/webhook/{path}
│       ├── Dockerfile
│       └── requirements.txt
├── infra/                         # Infrastructure backbone (no business logic)
│   └── caddy/
│       ├── Caddyfile              # Reverse proxy config (SITE_DOMAIN)
│       └── sites/
│           ├── ibkr.caddy         # SITE_DOMAIN route handlers (handle /ibkr/*)
│           └── debug.caddy        # Debug webhook routes (handle /debug/webhook/*)
└── types/                     # Type packages (TypeScript + Python)
    ├── typescript/            # @tradegist/ibkr-relay-types npm package
    │   ├── index.d.ts         # Barrel: exports Ibkr, IbkrPoller namespaces
    │   ├── package.json
    │   ├── shared/            # Ibkr namespace (CommonFill models)
    │   │   ├── index.d.ts
    │   │   └── types.d.ts     # Generated from services/shared/models.py (via schema_gen.py)
    │   └── poller/            # IbkrPoller namespace
    │       ├── index.d.ts
    │       └── types.d.ts     # Generated from services/poller/poller_models.py (via schema_gen.py)
    └── python/                # ibkr-relay-types PyPI package
        ├── pyproject.toml
        └── ibkr_relay_types/
            ├── __init__.py    # Re-exports all public types
            ├── shared.py      # Generated from services/shared/models.py
            └── poller.py      # Generated from poller_models.py

```

## Flex Web Service Setup

Before deploying, create an Activity Flex Query in IBKR Client Portal:

1. Log in to [Client Portal](https://portal.interactivebrokers.com)
2. Go to **Reporting** → **Flex Queries**
3. Under **Activity Flex Query**, click **+** to create a new query
4. Set **Period** to **Last 7 Days** (covers missed fills if the droplet was down)
5. In **Sections**, enable **Trades** and select the execution fields you want
6. Set **Format** to **XML**
7. Save and note the **Query ID** (use as `IBKR_FLEX_QUERY_ID`)
8. Go to **Flex Web Service Configuration** → enable and get the **Current Token** (use as `IBKR_FLEX_TOKEN`)

> **Why Activity instead of Trade Confirmation?** Trade Confirmation queries are locked to "Today" only. Activity queries support a configurable lookback period, so if the droplet is offline for a few days the first poll after restart will catch all missed fills. The SQLite dedup prevents double-sending.

## On-Demand Poll

Trigger an immediate poll without waiting for the next interval:

```bash
make poll
```

Additional flags:

```bash
make poll V=1                # verbose — run via SSH, see full poller logs
make poll DEBUG=1             # dump raw Flex XML (implies verbose)
make poll REPLAY=3            # resend 3 trades even if already processed (for testing)
make poll REPLAY=5 DEBUG=1    # combine flags
```

Or use the CLI directly:

```bash
python3 -m cli poll              # normal (HTTP)
python3 -m cli poll -v           # verbose (SSH)
python3 -m cli poll --debug      # raw XML
python3 -m cli poll --replay 3   # resend 3 trades
```

You can also call the endpoint directly with `curl`:

```bash
source .env && curl -s -X POST "https://${SITE_DOMAIN}/ibkr/poller/run" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  | python3 -m json.tool
```

You can optionally override the Flex token and query ID in the request body (defaults to the env vars if omitted):

```bash
curl -s -X POST "https://trade.example.com/ibkr/poller/run" \
  -H "Authorization: Bearer <API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"ibkr_flex_token": "abc", "ibkr_flex_query_id": "123"}' \
  | python3 -m json.tool
```

## Real-Time Listener

The relay includes an optional real-time listener that subscribes to [ibkr_bridge](https://github.com/tradegist/ibkr_bridge)'s WebSocket event stream for near-instant fill delivery — complementing the Flex poller (which runs every 10 minutes by default).

> **Prerequisite:** A running [ibkr_bridge](https://github.com/tradegist/ibkr_bridge) instance is required. The listener authenticates via the same `API_TOKEN` used for ibkr_bridge's HTTP API.

### Enabling the listener

Add the following to `.env`:

```env
LISTENER_ENABLED=true
BRIDGE_WS_URL=ws://bridge:5000/ibkr/ws/events   # container-to-container (same Docker network)
# BRIDGE_WS_URL=wss://trade.example.com/ibkr/ws/events  # cross-droplet (TLS)
BRIDGE_API_TOKEN=your_bridge_api_token            # must match bridge's API_TOKEN
```

Then run `make sync` to push the config and restart the `poller` container.

### Event types

The listener processes two event types from the bridge stream:

| Event                  | Default    | Description                                                                                                       |
| ---------------------- | ---------- | ----------------------------------------------------------------------------------------------------------------- |
| `commissionReportEvent`| **enabled**| Fired after commission is confirmed — contains the final fill with fee data. This is the primary fill event.      |
| `execDetailsEvent`     | disabled   | Fired immediately on execution — no commission data yet. Enable with `LISTENER_EXEC_EVENTS_ENABLED=true` for sub-second latency at the cost of 2× webhook volume (one preliminary + one confirmed per fill). |

### Operational notes

- **Only the primary `poller` service runs the listener.** The optional second poller (`poller-2`) does not start a listener — it uses the Flex poller only. This avoids double-delivery when both poller instances share the same IBKR account.
- **Dedup is shared with the Flex poller.** Both the listener and the Flex poller write to the same SQLite dedup database (`DEDUP_DB_PATH`). A fill delivered by the listener will be silently skipped if the Flex poller later sees the same `execId`, and vice versa.
- **Auto-reconnect with backoff.** On disconnect or error the listener waits (starting at 5 s, up to 5 min) and reconnects automatically. The last seen sequence number is sent on reconnect so the bridge can replay any missed events.
- **Debounce (optional).** Set `LISTENER_EVENT_DEBOUNCE_TIME` (milliseconds, default `0`) to buffer rapid partial fills before dispatching a single batched webhook. Useful when a large order fills in many small lots within a short window.

### Disabling the listener

Remove or comment out `LISTENER_ENABLED` (or set it to `false`) and run `make sync`. The listener task is not started on the next container restart.

## Pause & Resume

To stop billing for the droplet without losing state:

```bash
# Snapshot the droplet, unassign the reserved IP, delete the droplet
make pause

# Later — recreate the droplet from the snapshot and reassign the IP
make resume
```

**Costs while paused:**

- Droplet: **$0** (deleted)
- Snapshot: ~$0.06/GB/month (~$0.05/month for a fresh 25GB disk)
- Reserved IP: **$5/month** while unassigned (free when assigned to a droplet)

## SSH Access

```bash
make ssh
```

## Live Logs

Stream poller logs in real-time (useful for checking fill deliveries):

```bash
make logs                    # droplet (default: poller)
make logs S=poller-2         # second poller
make logs S=ibkr-debug       # debug inbox logs
```

Targets the droplet by default. Set `DEFAULT_CLI_RELAY_ENV=local` in `.env` (or pass `ENV=local`) to stream from the local stack instead:

```bash
make logs ENV=local          # local poller
make logs S=ibkr-debug       # local debug inbox (when DEFAULT_CLI_RELAY_ENV=local)
```

## Security

- Firewall restricts SSH (22) to the deployer's IP only
- HTTP/HTTPS open (Caddy auto-redirects HTTP → HTTPS)
- Webhook payloads are HMAC-SHA256 signed
- No credentials stored in the repository

## Current Status

- [x] Terraform infrastructure (droplet, firewall, SSH key)
- [x] Docker Compose orchestration (4 containers)
- [x] Flex poller with SQLite dedup + webhook delivery
- [x] On-demand poll endpoint (`make poll` / HTTP API)
- [x] Deploy/destroy/pause/resume scripts
- [x] Dry-run mode (log payloads when no webhook URL)
- [x] Webhook endpoint (HMAC-SHA256 signed, batched payloads)
- [x] Pluggable notification backends (`services/notifier/`, currently: webhook)
- [x] HTTPS via Caddy + Let's Encrypt
- [x] Makefile CLI (`make deploy`, `make poll`, etc.)
- [x] Unified Flex XML parsing (Activity + Trade Confirmation)
- [x] TypeScript type definitions (`@tradegist/ibkr-relay-types`, not yet published)
- [x] Optional second poller (`IBKR_FLEX_QUERY_ID_2`)
- [x] Debug webhook inbox (`DEBUG_WEBHOOK_PATH`)
- [ ] Health monitoring / alerting

## Flex XML Parsing

The poller supports both **Activity Flex Queries** (`<Trade>` tags) and **Trade Confirmation Flex Queries** (`<TradeConfirm>` / `<TradeConfirmation>` tags). To handle both formats in a unified way, the parser makes the following assumptions:

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

The XML parsing logic lives in [`services/poller/poller/flex_parser.py`](services/poller/poller/flex_parser.py).

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
