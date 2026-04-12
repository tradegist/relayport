# Broker Relay

A **relay between broker accounts** that provides clear, common interfaces to communicate with different brokers through a single interface layer — deployed to a DigitalOcean droplet with a single `make deploy`.

> [!WARNING]
> This project is under active development and not yet ready for prime time. You're welcome to use it, but expect frequent breaking changes.

## Why This Project?

Broker APIs are fragmented — each has its own data formats, auth patterns, and delivery mechanisms. Building the infrastructure to poll for fills, parse responses, deduplicate, and deliver webhooks takes time for **each** broker you want to integrate.

Broker Relay abstracts this away with a **relay adapter pattern**: one generic engine handles polling, dedup, aggregation, and webhook delivery, while broker-specific adapters handle the API quirks. Adding a new broker means writing one adapter — the infrastructure is already there.

Currently the project supports **IBKR** (Interactive Brokers) via the Flex Web Service and **Kraken** (crypto exchange) via the REST and WebSocket v2 APIs. It deploys to a DigitalOcean droplet (starting at **$4/month**) with:

- **A relay engine** that checks for trade fills and sends them to your webhook URL via a common payload format
- **Automatic HTTPS** via Caddy + Let's Encrypt
- **SQLite dedup** so each fill is delivered exactly once
- **A debug webhook inbox** for testing without hitting production services
- **Multi-account support** within each broker adapter
- **Optional real-time listeners** — IBKR via [ibkr_bridge](https://github.com/tradegist/ibkr_bridge) WebSocket, Kraken via native WS v2 executions channel

**Current direction:** Broker → User (trade fill events). Future plans include User → Broker communication (order placement).

> **Looking for order placement?** See [ibkr_bridge](https://github.com/tradegist/ibkr_bridge) — a companion project that runs the IB Gateway and exposes an HTTP API for placing orders and a real-time WebSocket event stream.

## Table of Contents

- [Quick Start](#quick-start)
- [API Endpoints](#api-endpoints)
- [Architecture](#architecture)
- [Configuration](#configuration)
- [Webhook Payload](#webhook-payload)
  - [Debug Webhook Inbox](#debug-webhook-inbox)
- [Flex Web Service Setup](#flex-web-service-setup) (IBKR)
- [Kraken Setup](#kraken-setup)
- [On-Demand Poll](#on-demand-poll)
- [Real-Time Listener (IBKR)](#real-time-listener-ibkr)
- [Commands](#commands)
- [Pause & Resume](#pause--resume)
- [Security](#security)
- [Testing](#testing)
- [TypeScript Types](#typescript-types)
- [Python Types](#python-types)
- [Project Structure](#project-structure)
- [Current Status](#current-status)
- [Flex XML Parsing](#flex-xml-parsing)
- [IBKR ID Reference](#ibkr-id-reference)

## Quick Start

A fully-fledged IBKR relay server on DigitalOcean in under 2 minutes:

```
git clone  →  make setup  →  set env vars  →  make deploy  →  trade fills hit your webhook
```

### Prerequisites

- [Docker Desktop](https://docs.docker.com/desktop/) (includes Docker Compose v2)
- [Terraform](https://developer.hashicorp.com/terraform/install)
- A [DigitalOcean API token](https://cloud.digitalocean.com/account/api/tokens)
- An IBKR account with Flex Web Service enabled (see [Flex Web Service Setup](#flex-web-service-setup))

**macOS / Linux** — install Docker Desktop and Terraform in one line:

```bash
# macOS (Homebrew)
brew install --cask docker && brew install terraform

# Linux (apt) — see links above for other distros
sudo apt-get install docker-compose-plugin && sudo apt-get install terraform
```

> **Windows** — install [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) and [Terraform](https://developer.hashicorp.com/terraform/install) manually or via `winget install Docker.DockerDesktop Hashicorp.Terraform`.

### Steps

```bash
# 1. Clone and set up
git clone https://github.com/tradegist/ibkr_relay.git
cd ibkr_relay
make setup                    # Create .venv, install deps, copy env templates

# 2. Configure (3 files)
#    .env          → RELAYS=ibkr, NOTIFIERS=webhook, TARGET_WEBHOOK_URL, WEBHOOK_SECRET
#    .env.droplet  → DEPLOY_MODE=standalone, DO_API_TOKEN
#    .env.relays   → IBKR_FLEX_TOKEN, IBKR_FLEX_QUERY_ID

# 3. Deploy — provisions a droplet, starts all containers
make deploy

# That's it. Trade fills now arrive at your webhook URL.
# DNS and HTTPS can be configured later — the relay polls and delivers
# webhooks immediately, no inbound access needed.

# Tear down when done
make destroy
```

## API Endpoints

All endpoints require `Authorization: Bearer <API_TOKEN>` header (except health).

#### Trigger a poll

```
POST /relays/{relay_name}/poll/{poll_idx}
```

No body required. Immediately polls the broker for new fills and sends them to the configured webhook. `poll_idx` is 1-based (e.g. `/relays/ibkr/poll/1` for the primary poller, `/relays/ibkr/poll/2` for the second account).

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
│  │  trade.example.com → relays:8000             │        │
│  │  Ports: 80 (HTTP→redirect), 443 (HTTPS)      │        │
│  └──────────────┬──────────────────┬────────────┘        │
│                 │                  │                     │
│  ┌──────────────▼───────┐  ┌───────▼─────────────────┐   │
│  │  relays              │  │  debug (optional)       │   │
│  │  Registry → Adapters │  │  Webhook payload inbox  │   │
│  │  Poller engine       │  │  POST/GET/DELETE        │   │
│  │  Listener engine     │  └─────────────────────────┘   │
│  │  HTTP API            │                                │
│  │  SQLite dedup        │                                │
│  └──────────────────────┘                                │
│                                                          │
│  Firewall: SSH from deployer IP only                     │
│  HTTP/HTTPS open (Caddy auto-redirects HTTP → HTTPS)     │
└──────────────────────────────────────────────────────────┘
```

Three containers in a single Docker network (debug is optional):

- **`caddy`** — [Caddy 2](https://caddyserver.com/) reverse proxy with automatic HTTPS via Let's Encrypt. Routes `/relays/*` to the relays service.
- **`relays`** — Multi-relay service that loads broker adapters via the registry pattern. Runs pollers (periodic Flex fetch), an optional real-time WebSocket listener, and an HTTP API. Each broker adapter is a plugin that provides fetch/parse callbacks — the generic engines handle dedup, aggregation, notification, and scheduling. **Does not hold any broker sessions** — trade normally via web/mobile.
- **`debug`** — Optional debug webhook inbox. Captures webhook payloads for inspection during development. Enabled when `DEBUG_WEBHOOK_PATH` is set.

> **Dedup guarantee.** The relay uses a SQLite dedup database so each fill is delivered at most once under normal operation. In the rare event of an internal crash between webhook delivery and dedup bookkeeping, a fill may be sent a second time. Design your webhook consumer to be idempotent (e.g. deduplicate on `execId`).

## Domains & HTTPS

> **Not required to get started.** The relay's core job is entirely outbound — poll the broker, send webhooks. It works immediately after `make deploy` without DNS or HTTPS. A domain is only needed for inbound access: on-demand polls via the API, health checks, and the debug webhook inbox.

Set `SITE_DOMAIN` and `API_TOKEN` in `.env` when you're ready for inbound access. Caddy uses the domain to automatically provision a TLS certificate from Let's Encrypt.

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

Set `DROPLET_SIZE` in `.env.droplet` to control the droplet size. The relay is lightweight — the smallest droplet ($4/month) works fine:

```env
DROPLET_SIZE=s-1vcpu-512mb   # $4/month (default)
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

## TypeScript Types

Webhook payload types are available as a TypeScript package under `types/typescript/`:

```
types/typescript/
  index.d.ts                 # Barrel: exports BrokerRelay, RelayApi namespaces
  package.json               # @tradegist/broker-relay-types
  shared/
    index.d.ts               # Re-exports: BuySell, Fill, Trade, WebhookPayloadTrades, WebhookPayload
    types.d.ts               # Generated from services/shared/models.py (via schema_gen.py)
    types.schema.json         # Intermediate JSON Schema
  relay_api/
    index.d.ts               # Re-exports: RunPollResponse, HealthResponse
    types.d.ts               # Generated from services/relay_core/relay_models.py (via schema_gen.py)
    types.schema.json         # Intermediate JSON Schema
```

Usage:

```typescript
import { BrokerRelay, RelayApi } from "@tradegist/broker-relay-types";

const payload: BrokerRelay.WebhookPayload = ...;   // discriminated union (use this for consumers)
const poll: RelayApi.RunPollResponse = ...;         // relay API types
```

Types are auto-generated from the Pydantic models via `make types`. The `Trade` type follows the CommonFill contract (`orderId`, `symbol`, `side`, `volume`, `price`, `fee`, `cost`, `orderType`, `timestamp`, `source`, `raw`, `fillCount`, `execIds`). The package is not yet published to npm — the API is still evolving.

## Python Types

Pydantic models are also available as a standalone Python package under `types/python/`:

```
types/python/
  pyproject.toml              # broker-relay-types, deps: pydantic
  broker_relay_types/
    __init__.py               # Re-exports all public types
    shared.py                 # CommonFill models (generated from services/shared/models.py)
    poller.py                 # Relay API types (generated from relay_models.py)
```

Usage:

```python
from broker_relay_types import Fill, Trade, WebhookPayload, BuySell
```

Auto-generated by `gen_python_types.py` — run `make types` to regenerate. Do not edit the generated files manually.

## Configuration

Configuration is split across three environment files. Templates are in `env_examples/` — `make setup` copies them to `.<name>` if missing.

### `.env` — App config

| Variable                       | Required | Default            | Description                                                                                                     |
| ------------------------------ | -------- | ------------------ | --------------------------------------------------------------------------------------------------------------- |
| `SITE_DOMAIN`                  | Yes      | —                  | Domain for the relay API (see [Domains & HTTPS](#domains--https))                                               |
| `API_TOKEN`                    | Yes      | —                  | Bearer token for `/relays/*` endpoints (`openssl rand -hex 32`)                                                 |
| `RELAYS`                       | No       | —                  | Comma-separated relay adapters (e.g. `ibkr`, `ibkr,kraken`). Empty = API server only                            |
| `NOTIFIERS`                    | No       | —                  | Active notification backends (e.g. `webhook`). Empty = dry-run                                                  |
| `TARGET_WEBHOOK_URL`           | No       | —                  | Webhook endpoint (empty = log-only dry-run)                                                                     |
| `WEBHOOK_SECRET`               | No       | —                  | HMAC-SHA256 key for signing payloads (required if NOTIFIERS=webhook)                                            |
| `POLL_INTERVAL`                | No       | `600`              | Flex poll interval (seconds)                                                                                    |
| `LISTENER_ENABLED`             | No       | —                  | Set to `true` to enable real-time WS listener (requires ibkr_bridge)                                            |
| `LISTENER_DEBOUNCE_MS`         | No       | `0`                | Milliseconds to buffer fills before flushing                                                                    |
| `IBKR_LISTENER_EXEC_EVENTS_ENABLED` | No       | `false`            | Enable `execDetailsEvent` webhooks (2x volume, lower latency)                                                   |
| `DEBUG_WEBHOOK_PATH`           | No       | —                  | Route webhooks to debug inbox instead of `TARGET_WEBHOOK_URL` (see [Debug Webhook Inbox](#debug-webhook-inbox)) |
| `MAX_DEBUG_WEBHOOK_PAYLOADS`   | No       | `100`              | Max payloads stored in the debug inbox (hard max: 150, FIFO eviction)                                           |
| `DEBUG_LOG_LEVEL`              | No       | `INFO`             | Set to `DEBUG` to include full payload+headers in `docker logs debug`                                           |
| `TIME_ZONE`                    | No       | `America/New_York` | Timezone (tz database format)                                                                                   |

### `.env.droplet` — CLI-only (never pushed to containers)

| Variable       | Required | Default               | Description                                                                  |
| -------------- | -------- | --------------------- | ---------------------------------------------------------------------------- |
| `DEPLOY_MODE`  | Yes      | —                     | `standalone` (own droplet via Terraform) or `shared` (existing droplet)      |
| `DO_API_TOKEN` | Yes\*    | —                     | DigitalOcean API token (standalone only — can be removed after first deploy) |
| `DROPLET_IP`   | Yes\*    | —                     | Droplet IP (from Terraform output in standalone; provided by host in shared) |
| `SSH_KEY`      | No       | `~/.ssh/broker-relay` | SSH key path — **shared mode only**. Standalone auto-generates.              |
| `DROPLET_SIZE` | No       | `s-1vcpu-512mb`       | Override droplet size slug                                                   |

### `.env.relays` — Relay-prefixed vars

| Variable                  | Required | Description                                                       |
| ------------------------- | -------- | ----------------------------------------------------------------- |
| `IBKR_FLEX_TOKEN`         | Yes      | Flex Web Service token (from Client Portal)                       |
| `IBKR_FLEX_QUERY_ID`      | Yes      | Flex Query ID (Trade Confirmation or Activity)                    |
| `IBKR_FLEX_QUERY_ID_2`    | No       | Second account query ID (enables second poller within same relay) |
| `IBKR_FLEX_TOKEN_2`       | No       | Second account token (defaults to primary if omitted)             |
| `IBKR_NOTIFIERS`          | No       | Override `NOTIFIERS` for IBKR relay only                          |
| `IBKR_TARGET_WEBHOOK_URL` | No       | Override `TARGET_WEBHOOK_URL` for IBKR relay only                 |
| `IBKR_WEBHOOK_SECRET`     | No       | Override `WEBHOOK_SECRET` for IBKR relay only                     |
| `IBKR_POLL_INTERVAL`      | No       | Override `POLL_INTERVAL` for IBKR relay only                      |
| **Kraken**                |          |                                                                   |
| `KRAKEN_API_KEY`          | Yes\*    | Kraken API key (required when `kraken` is in `RELAYS`)            |
| `KRAKEN_API_SECRET`       | Yes\*    | Kraken API secret, base64-encoded (required with API key)         |
| `KRAKEN_NOTIFIERS`        | No       | Override `NOTIFIERS` for Kraken relay only                        |
| `KRAKEN_TARGET_WEBHOOK_URL` | No     | Override `TARGET_WEBHOOK_URL` for Kraken relay only               |
| `KRAKEN_WEBHOOK_SECRET`   | No       | Override `WEBHOOK_SECRET` for Kraken relay only                   |
| `KRAKEN_POLL_INTERVAL`    | No       | Override `POLL_INTERVAL` for Kraken relay only                    |

Adding a new relay's vars requires no compose changes — just add prefixed vars to `.env.relays`.

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
      "fee": 1.0,
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

The envelope uses a discriminated union pattern — `relay` identifies the broker and `type` identifies the event kind. Consumers should type their variables as `WebhookPayload` (the union). Currently the only variant is `WebhookPayloadTrades` (`type: "trades"`); new event types (e.g. orders, positions) will be added as new variants.

### CommonFill Contract

All broker adapters use the same **CommonFill** model. The `data` array contains `Trade` objects with these guaranteed fields:

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
| `fee`        | `number`            | Total fees/commissions (always positive — amount paid)                                    |
| `fillCount`  | `number`            | Number of fills aggregated into this trade                                                |
| `execIds`    | `string[]`          | One execution ID per fill (for tracing back to individual fills)                          |
| `timestamp`  | `string`            | Latest fill timestamp                                                                     |
| `source`     | `string`            | Origin: `"flex"` (IBKR Flex poll), `"execDetailsEvent"` / `"commissionReportEvent"` (IBKR WS), `"rest_poll"` (Kraken REST), `"ws_execution"` (Kraken WS) |
| `raw`        | `object`            | Original broker-specific payload (all fields, unmodified)                                 |

The `raw` object preserves the full broker-specific data. For IBKR Flex, this includes ~100 XML attributes (account info, security details, financial fields, dates). Consumers should treat `raw` as opaque broker data — the CommonFill fields above are the stable contract.

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

To test webhook delivery without hitting production services, set `DEBUG_WEBHOOK_PATH` in `.env`:

```env
DEBUG_WEBHOOK_PATH=abcdef
#MAX_DEBUG_WEBHOOK_PAYLOADS=100
#DEBUG_LOG_LEVEL=INFO
```

This starts the `debug` container and reroutes all webhook delivery to an in-memory inbox at `/debug/webhook/<path>`. The real `TARGET_WEBHOOK_URL` is ignored while this is set.

**Inspect captured payloads:**

```bash
# View all stored payloads
curl -s https://trade.example.com/debug/webhook/abcdef | python3 -m json.tool

# Clear the inbox
curl -s -X DELETE https://trade.example.com/debug/webhook/abcdef
```

**Stream payloads in real time** — set `DEBUG_LOG_LEVEL=DEBUG` and tail the container logs:

```bash
make logs S=debug
# or: docker logs -f debug
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
  make sync        Push .env + .env.relays + restart (S=service B=1 LOCAL_FILES=1 ENV=local)
  make poll        Trigger an immediate poll (RELAY=ibkr IDX=1 V=1 REPLAY=N)
  make test-webhook Send sample trades to webhook endpoint
  make types       Regenerate TypeScript + Python types from Pydantic models
  make test        Run unit tests (pytest)
  make typecheck   Run mypy strict type checking
  make lint        Run ruff linter (FIX=1 to auto-fix)
  make e2e         Run E2E tests (starts/stops stack automatically)
  make e2e-up      Start E2E test stack (relays + debug)
  make e2e-run     Run E2E tests (stack must be up)
  make e2e-down    Stop and remove E2E test stack
  make local-up    Start full stack locally (no TLS, direct port access)
  make local-down  Stop local stack
  make logs        Stream logs (S=service ENV=local, default: relays on droplet)
  make stats       Show container resource usage
  make ssh         SSH into the droplet
  make help        Show available commands
```

You can also invoke the CLI directly with `python3 -m cli <command>` — useful on Windows or when Make is not available:

> [!NOTE]
> Most commands work on Windows, but `sync --local-files` requires `rsync` and SSH, which are only available natively on macOS and Linux. On Windows, use [WSL](https://learn.microsoft.com/en-us/windows/wsl/).

```bash
python3 -m cli deploy
python3 -m cli sync
python3 -m cli poll ibkr 1            # primary poller
python3 -m cli poll ibkr 2            # second account
python3 -m cli test-webhook           # send sample trades to webhook
python3 -m cli test-webhook 2         # send to second webhook
python3 -m cli pause
python3 -m cli resume
python3 -m cli destroy
```

`make` examples:

```bash
make deploy                                    # provision droplet + start containers
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
make test                                      # run unit tests
make typecheck                                 # strict mypy checking
make logs                                      # stream relays logs (droplet)
make logs S=debug                              # stream debug inbox logs
make logs ENV=local                            # stream local stack logs
```

```bash
make pause                           # snapshot + delete droplet
make resume                          # restore from snapshot
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
│   │   ├── shared/                # BrokerRelay namespace (CommonFill models)
│   │   │   ├── index.d.ts
│   │   │   └── types.d.ts         # Generated from services/shared/models.py
│   │   └── relay_api/             # RelayApi namespace
│   │       ├── index.d.ts
│   │       └── types.d.ts         # Generated from services/relay_core/relay_models.py
│   └── python/                    # broker-relay-types PyPI package
│       ├── pyproject.toml
│       └── broker_relay_types/
│           ├── __init__.py        # Re-exports all public types
│           ├── shared.py          # Generated from services/shared/models.py
│           └── poller.py          # Generated from relay_models.py
├── schema_gen.py                  # JSON Schema generator (Pydantic → TS types)
└── gen_python_types.py            # Python types generator (models → types/python/)
```

## Flex Web Service Setup

Before deploying, create an Activity Flex Query in IBKR Client Portal:

1. Log in to [Client Portal](https://portal.interactivebrokers.com)
2. Go to **Reporting** → **Flex Queries**
3. Under **Activity Flex Query**, click **+** to create a new query
4. Set **Period** to **Last 7 Days** (covers missed fills if the droplet was down)
5. In **Sections**, enable **Trades** and select the execution fields you want
6. Set **Format** to **XML**
7. Save and note the **Query ID** (use as `IBKR_FLEX_QUERY_ID` in `.env.relays`)
8. Go to **Flex Web Service Configuration** → enable and get the **Current Token** (use as `IBKR_FLEX_TOKEN` in `.env.relays`)

> **Why Activity instead of Trade Confirmation?** Trade Confirmation queries are locked to "Today" only. Activity queries support a configurable lookback period, so if the droplet is offline for a few days the first poll after restart will catch all missed fills. The SQLite dedup prevents double-sending.

## Kraken Setup

To add Kraken as a relay:

1. Create API credentials at [Kraken](https://www.kraken.com/) under **Settings** > **API**
2. Required permissions: **Query Funds**, **Query Open Orders & Trades**, **Query Closed Orders & Trades**, **Access WebSockets API**
3. Add to `.env`:
   ```env
   RELAYS=kraken              # or RELAYS=ibkr,kraken for both
   ```
4. Add to `.env.relays`:
   ```env
   KRAKEN_API_KEY=your_api_key
   KRAKEN_API_SECRET=your_base64_encoded_secret
   ```
5. Run `make sync` to push config and restart.

### Kraken polling

The Kraken poller calls the `TradesHistory` REST endpoint at the configured interval (default: 600s). Override with `KRAKEN_POLL_INTERVAL` in `.env.relays`.

### Kraken real-time listener

Enable the WebSocket v2 listener for near-instant fill delivery:

```env
# .env
KRAKEN_LISTENER_ENABLED=true
```

The listener connects to `wss://ws-auth.kraken.com/v2`, subscribes to the `executions` channel, and pushes fills to your webhook as they execute. No external bridge required — Kraken's native WS API is used directly.

### Webhook payload example (Kraken)

```json
{
  "relay": "kraken",
  "type": "trades",
  "data": [
    {
      "orderId": "OXXXXX-XXXXX-XXXXXX",
      "symbol": "XETHZUSD",
      "assetClass": "crypto",
      "side": "buy",
      "orderType": "limit",
      "price": 2450.50,
      "volume": 0.5,
      "cost": 1225.25,
      "fee": 0.32,
      "fillCount": 1,
      "execIds": ["TID-XXXXX-XXXXX"],
      "timestamp": "2026-04-12T15:30:00Z",
      "source": "rest_poll",
      "raw": { "txid": "TID-XXXXX-XXXXX", "pair": "XETHZUSD", "...": "..." }
    }
  ],
  "errors": []
}
```

## On-Demand Poll

Trigger an immediate poll without waiting for the next interval:

```bash
make poll
```

Additional flags:

```bash
make poll RELAY=ibkr IDX=2    # second account
make poll V=1                 # verbose — stream container logs alongside poll
make poll REPLAY=3            # resend 3 trades even if already processed (for testing)
make poll REPLAY=5 V=1        # combine flags
```

Or use the CLI directly:

```bash
python3 -m cli poll ibkr 1          # normal (HTTP)
python3 -m cli poll ibkr 1 -v       # verbose (stream logs)
python3 -m cli poll ibkr 1 --replay 3  # resend 3 trades
```

You can also call the endpoint directly with `curl`:

```bash
source .env && curl -s -X POST "https://${SITE_DOMAIN}/relays/ibkr/poll/1" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  | python3 -m json.tool
```

## Real-Time Listener (IBKR)

The IBKR relay includes an optional real-time listener that subscribes to [ibkr_bridge](https://github.com/tradegist/ibkr_bridge)'s WebSocket event stream for near-instant fill delivery — complementing the Flex poller (which runs every 10 minutes by default).

> **Note:** Kraken has its own native WebSocket listener — see [Kraken Setup](#kraken-setup) for details.

> **Prerequisite:** A running [ibkr_bridge](https://github.com/tradegist/ibkr_bridge) instance is required. The listener authenticates via the bridge's `API_TOKEN`.

### Enabling the listener

Add the following to `.env`:

```env
LISTENER_ENABLED=true
```

And set the bridge connection vars in `.env.relays`:

```env
IBKR_BRIDGE_WS_URL=ws://bridge:5000/ibkr/ws/events   # container-to-container (same Docker network)
# IBKR_BRIDGE_WS_URL=wss://trade.example.com/ibkr/ws/events  # cross-droplet (TLS)
IBKR_BRIDGE_API_TOKEN=your_bridge_api_token              # must match bridge's API_TOKEN
```

Then run `make sync` to push the config and restart the `relays` container.

### Event types

The listener processes two event types from the bridge stream:

| Event                   | Default     | Description                                                                                                                                                                                                  |
| ----------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `commissionReportEvent` | **enabled** | Fired after commission is confirmed — contains the final fill with fee data. This is the primary fill event.                                                                                                 |
| `execDetailsEvent`      | disabled    | Fired immediately on execution — no commission data yet. Enable with `IBKR_LISTENER_EXEC_EVENTS_ENABLED=true` for sub-second latency at the cost of 2× webhook volume (one preliminary + one confirmed per fill). |

### Operational notes

- **Dedup is shared with the Flex poller.** Both the listener and the Flex poller write to the same SQLite dedup database. A fill delivered by the listener will be silently skipped if the Flex poller later sees the same `execId`, and vice versa.
- **Auto-reconnect with backoff.** On disconnect or error the listener waits (starting at 5 s, up to 5 min) and reconnects automatically. The last seen sequence number is sent on reconnect so the bridge can replay any missed events.
- **Debounce (optional).** Set `LISTENER_DEBOUNCE_MS` (milliseconds, default `0`) to buffer rapid partial fills before dispatching a single batched webhook. Useful when a large order fills in many small lots within a short window.

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

Stream relay logs in real-time (useful for checking fill deliveries):

```bash
make logs                    # droplet (default: relays)
make logs S=debug            # debug inbox logs
```

Targets the droplet by default. Set `DEFAULT_CLI_RELAY_ENV=local` in `.env` (or pass `ENV=local`) to stream from the local stack instead:

```bash
make logs ENV=local          # local relays
make logs S=debug ENV=local  # local debug inbox
```

## Security

- Firewall restricts SSH (22) to the deployer's IP only
- HTTP/HTTPS open (Caddy auto-redirects HTTP → HTTPS)
- Webhook payloads are HMAC-SHA256 signed
- No credentials stored in the repository

## Current Status

- [x] Terraform infrastructure (droplet, firewall, SSH key)
- [x] Docker Compose orchestration (3 containers)
- [x] Multi-relay registry pattern (IBKR, Kraken)
- [x] Flex poller with SQLite dedup + webhook delivery
- [x] On-demand poll endpoint (`make poll` / HTTP API)
- [x] Deploy/destroy/pause/resume scripts
- [x] Dry-run mode (log payloads when no webhook URL)
- [x] Webhook endpoint (HMAC-SHA256 signed, batched payloads)
- [x] Pluggable notification backends (currently: webhook)
- [x] HTTPS via Caddy + Let's Encrypt
- [x] Makefile CLI (`make deploy`, `make poll`, etc.)
- [x] Unified Flex XML parsing (Activity + Trade Confirmation)
- [x] TypeScript type definitions (`@tradegist/broker-relay-types`, not yet published)
- [x] Python type definitions (`broker-relay-types`, not yet published)
- [x] Multi-account support within each relay (`_2` suffix)
- [x] Debug webhook inbox (`DEBUG_WEBHOOK_PATH`)
- [x] Real-time listener (ibkr_bridge WebSocket)
- [x] Env file split (`.env` + `.env.droplet` + `.env.relays`)
- [ ] Health monitoring / alerting
- [x] Kraken crypto exchange adapter (REST poller + WS v2 listener)
- [ ] Additional broker adapters

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
