# IBKR Webhook Relay

Deploy a fully functional **Interactive Brokers Gateway API** to your own server with a few environment variables and one command.

> [!WARNING]
> This project is under active development and not yet ready for prime time. You're welcome to use it, but expect frequent breaking changes.

## Why This Project?

IBKR has a notoriously difficult API. To automate anything — placing orders, getting fill confirmations — you need to run their Java-based Gateway or TWS application. That means either keeping it running on your local machine (impractical for web apps or any always-on service) or setting it up on a remote server (surprisingly painful to get right).

Thankfully, amazing open-source projects like [`ib_async`](https://github.com/ib-api-reloaded/ib_async), [`ib-gateway-docker`](https://github.com/gnzsnz/ib-gateway-docker), and others have made the core pieces much more accessible. This project wouldn't exist without them.

But even with those libraries, you still need to **build a Python app, deploy it somewhere, handle HTTPS, 2FA, reconnections, and webhooks**. That's where this project comes in — it bundles everything into a single `make deploy` that provisions a DigitalOcean droplet (starting at $12/month) with:

- **An HTTPS endpoint to place orders** via a simple REST API
- **A poller** that checks for trade fills every 10 minutes and sends them to your webhook URL
- **A real-time listener** (opt-in) that fires webhooks immediately when orders fill via IB Gateway events

> **Only one endpoint for now?** Yes — more APIs will be exposed as the need arises. PRs welcome.

> **Why both a poller and a listener?** The poller uses the [Flex Web Service](https://www.interactivebrokers.com/campus/ibkr-api-page/flex-web-service/) (a REST API) — it **does not require an active Gateway session**. You can close the gateway, trade normally via web/mobile, and know that ~10 minutes later the poller will catch any fills. The listener gives you **instant** webhooks (sub-second) but only works while the Gateway is connected. Use the poller as your reliable baseline, and optionally enable the listener for real-time notifications when the Gateway is up.

## Table of Contents

- [API Endpoints](#api-endpoints)
- [Architecture](#architecture)
- [Quick Start](#quick-start-local-deploy)
- [Configuration](#configuration)
- [Domains & HTTPS](#domains--https)
- [Memory & Droplet Sizing](#memory--droplet-sizing)
- [Gateway Management](#gateway-management)
- [Placing Orders](#placing-orders)
- [Webhook Payload](#webhook-payload)
- [Flex Web Service Setup](#flex-web-service-setup)
- [On-Demand Poll](#on-demand-poll)
- [Commands](#commands)
- [Pause & Resume](#pause--resume)
- [Security](#security)
- [Testing](#testing)
- [TypeScript Types](#typescript-types)
- [GitHub Actions](#github-actions-fork--deploy)
- [Project Structure](#project-structure)
- [Current Status](#current-status)
- [Flex XML Parsing](#flex-xml-parsing)
- [Real-Time Listener](#real-time-listener)

## API Endpoints

All endpoints require `Authorization: Bearer <API_TOKEN>` header.

#### Place an order

```
POST /ibkr/order
```

```json
{
  "contract": {
    "symbol": "TSLA",
    "secType": "STK",
    "exchange": "SMART",
    "currency": "USD"
  },
  "order": {
    "action": "BUY",
    "totalQuantity": 2,
    "orderType": "MKT"
  }
}
```

#### Trigger a poll

```
POST /ibkr/poller/run
```

No body required. Immediately polls the Flex Web Service for new fills and sends them to the configured webhook.

#### Health check

```
GET /health
```

Returns `{"connected": true, "tradingMode": "paper"}` when the relay has an active connection to IB Gateway, `false` during reconnection (e.g. after a gateway restart). No auth required.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  DigitalOcean Droplet (auto-sized based on JAVA_HEAP_SIZE)   │
│                                                              │
│  ┌─────────────────┐   Docker    ┌─────────────────────────┐ │
│  │  ib-gateway     │  Network   │  remote-client           │ │
│  │  gnzsnz/ib-gw   │◄──────────►│  Python 3.11             │ │
│  │  API: 4003/4004 │            │  ib_async + HTTP API     │ │
│  │  VNC: 5900      │            │  (order placement)       │ │
│  └────────┬────────┘            └───────────▲──────────────┘ │
│           │                                 │                │
│  ┌────────▼─────────┐            ┌──────────┴──────────────┐ │
│  │  novnc           │            │  poller                 │ │
│  │  Browser VNC     │            │  Flex Web Service       │ │
│  │  (2FA access)    │            │  → Webhook POST         │ │
│  └────────▲─────────┘            │  SQLite dedup           │ │
│           │                      └─────────────────────────┘ │
│  ┌────────┴─────────────────────────────────┐                │
│  │  caddy (reverse proxy + auto HTTPS)      │                │
│  │  vnc.example.com   → novnc:8080          │                │
│  │  trade.example.com → remote-client:5000  │                │
│  │  Ports: 80 (HTTP→redirect), 443 (HTTPS)  │                │
│  └──────────────────────────────────────────┘                │
│                                                              │
│  Firewall: SSH from deployer IP only                         │
│  HTTP/HTTPS open (Caddy auto-redirects HTTP → HTTPS)         │
│  IBKR API ports are internal-only (not exposed)              │
└──────────────────────────────────────────────────────────────┘
```

Six containers in a single Docker network:

- **`ib-gateway`** — [`ghcr.io/gnzsnz/ib-gateway:stable`](https://github.com/gnzsnz/ib-gateway-docker). IBC automates login. VNC on port 5900 (raw), API on 4003 (live) / 4004 (paper).
- **`novnc`** — [`theasp/novnc`](https://hub.docker.com/r/theasp/novnc). Browser-based VNC proxy for completing 2FA.
- **`caddy`** — [Caddy 2](https://caddyserver.com/) reverse proxy with automatic HTTPS via Let's Encrypt. Routes traffic to the correct backend based on domain (see [Domains & HTTPS](#domains--https)).
- **`remote-client`** — Python image connected to IB Gateway via `ib_async`. Exposes an HTTP API (internal port 5000) for placing stock orders, secured with Bearer token authentication.
- **`poller`** — Python image that polls the IBKR Flex Web Service every 10 minutes for new fills and sends them via pluggable notification backends (see `services/notifier/`). Supports both **Trade Confirmation** and **Activity** Flex Query types. Uses SQLite for deduplication. **Does not hold an IBKR session** — trade normally via web/mobile.
- **`gateway-controller`** — Lightweight Alpine sidecar with Docker CLI. Exposes a CGI endpoint so the noVNC page can start the gateway container from the browser.

## Domains & HTTPS

Two domain names are **required**. Caddy uses them to automatically provision TLS certificates from Let's Encrypt, providing secure HTTPS connections. Without valid domains, Caddy cannot obtain certificates and the services will not be accessible — there is no fallback to plain HTTP or IP-based access.

| Environment Variable | Purpose                                                      | Example             |
| -------------------- | ------------------------------------------------------------ | ------------------- |
| `VNC_DOMAIN`         | Serves the noVNC interface for IB Gateway 2FA authentication | `vnc.example.com`   |
| `SITE_DOMAIN`        | Serves the order placement API (`/ibkr/order`, `/health`)    | `trade.example.com` |

### Setup

1. Point **both** domains to the droplet's reserved IP as **A records**:
   ```
   vnc.example.com    A    181.66.270.412
   trade.example.com  A    181.66.270.412
   ```
2. Set both in `.env`:
   ```
   VNC_DOMAIN=vnc.example.com
   SITE_DOMAIN=trade.example.com
   ```
3. Start the stack — Caddy will automatically obtain and renew certificates for both domains.

> **Why two domains?** The VNC interface provides direct access to IB Gateway for 2FA and manual management. The trade API is a separate concern with its own authentication (Bearer token). Separating them on different domains provides clean isolation — you can restrict VNC access at the DNS/firewall level without affecting the trade API, and vice versa.

> **Can I use just an IP address?** No. Let's Encrypt does not issue certificates for bare IP addresses. The Caddy reverse proxy requires valid domain names to provision TLS certificates. Both `VNC_DOMAIN` and `SITE_DOMAIN` must be set or the stack will refuse to start.

## Memory & Droplet Sizing

IB Gateway runs on Java and its performance depends on the heap memory allocation. Set `JAVA_HEAP_SIZE` in `.env` (in MB) to control this. The droplet size is **automatically selected** to fit the requested heap plus OS/Docker overhead:

| `JAVA_HEAP_SIZE` | Droplet Size   | RAM   | Approx. Cost |
| ---------------- | -------------- | ----- | ------------ |
| ≤ 1024 (default) | `s-1vcpu-2gb`  | 2 GB  | ~$12/mo      |
| 1025 – 3072      | `s-2vcpu-4gb`  | 4 GB  | ~$24/mo      |
| 3073 – 6144      | `s-4vcpu-8gb`  | 8 GB  | ~$48/mo      |
| 6145 – 10240     | `s-8vcpu-16gb` | 16 GB | ~$96/mo      |

IBKR recommends **4096** for API users. The default (768) works but may be slow for data-heavy operations.

```env
# .env
JAVA_HEAP_SIZE=4096
```

## Quick Start (Local Deploy)

### Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/install) installed
- A [DigitalOcean API token](https://cloud.digitalocean.com/account/api/tokens)
- An IBKR account (paper or live)

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

# 3. Complete 2FA
# Open the VNC URL printed by deploy in your browser
# Log in and approve the 2FA prompt

# 4. Tear down when done
make destroy
```

## Testing

### Unit tests

```bash
make test        # run pytest (poller, parser, models)
make typecheck   # strict mypy checking
```

### E2E tests

End-to-end tests run against a local Docker stack with a real IB Gateway connected to a **paper trading account**. Real orders are placed in paper mode.

**Setup:**

1. Copy the template and fill in your paper account credentials:

   ```bash
   cp .env.test.example .env.test
   # Edit .env.test with your IBKR paper username and password
   ```

2. Make sure Docker Desktop is running.

3. Run the tests:

   ```bash
   make e2e          # start stack → run tests → tear down
   ```

   Or manage the stack manually:

   ```bash
   make e2e-up       # start ib-gateway + remote-client (paper mode)
   make e2e-run      # run tests (stack must be up)
   make e2e-down     # stop and remove containers
   ```

The test stack exposes the API on `http://localhost:15010` with a hardcoded token (`test-token`). The gateway typically connects within ~20 seconds.

### Local production stack

Run the full production stack on your local machine — no TLS, no Caddy, direct port access:

```bash
make local-up     # build and start all services
make local-down   # stop and remove containers
```

Endpoints after startup:

| Service   | URL                           |
| --------- | ----------------------------- |
| REST API  | http://localhost:15000/health |
| Poller    | http://localhost:15001/health |
| VNC (2FA) | http://localhost:15002        |

If you change `.env`, refresh the running stack without rebuilding:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
```

## TypeScript Types

Webhook payload and order placement types are available as a TypeScript package under `types/`:

```
types/
  index.d.ts                 # Barrel: exports IbkrPoller, IbkrHttp namespaces
  package.json               # @tradegist/ibkr-types
  poller/
    index.d.ts               # Re-exports: BuySell, WebhookPayload, Trade
    types.d.ts               # Generated from services/poller/models_poller.py
    types.schema.json         # Intermediate JSON Schema
  http/
    index.d.ts               # Re-exports: PlaceOrderPayload, ContractPayload, OrderPayload, PlaceOrderResponse
    types.d.ts               # Generated from services/remote-client/models_remote_client.py
    types.schema.json         # Intermediate JSON Schema
```

Usage:

```typescript
import { IbkrPoller, IbkrHttp } from "@tradegist/ibkr-types";

const payload: IbkrPoller.WebhookPayload = ...;
const order: IbkrHttp.PlaceOrderPayload = ...;
```

Types are auto-generated from the Pydantic models via `make types`. The package is not yet published to npm — the API is still evolving.

## GitHub Actions (Fork & Deploy)

For automated deployment without local Terraform:

1. Fork this repo
2. Create a [DO Spaces](https://cloud.digitalocean.com/spaces) bucket for Terraform state
3. Add these **GitHub Secrets**:

| Secret                  | Description                                   |
| ----------------------- | --------------------------------------------- |
| `DO_API_TOKEN`          | DigitalOcean API token                        |
| `TWS_USERID`            | IBKR username                                 |
| `TWS_PASSWORD`          | IBKR password                                 |
| `VNC_SERVER_PASSWORD`   | Password for browser VNC access               |
| `VNC_DOMAIN`            | Domain for VNC access                         |
| `SITE_DOMAIN`           | Domain for trade API                          |
| `API_TOKEN`             | Bearer token for trade API                    |
| `IBKR_FLEX_TOKEN`       | Flex Web Service token                        |
| `IBKR_FLEX_QUERY_ID`    | Trade Confirmation query ID                   |
| `TARGET_WEBHOOK_URL`    | Webhook destination (leave empty for dry-run) |
| `WEBHOOK_SECRET`        | HMAC-SHA256 signing key                       |
| `TRADING_MODE`          | `paper` or `live`                             |
| `POLL_INTERVAL_SECONDS` | Poll frequency (default: 600)                 |
| `TIME_ZONE`             | e.g. `America/New_York`                       |
| `SPACES_ACCESS_KEY`     | DO Spaces access key (for TF state)           |
| `SPACES_SECRET_KEY`     | DO Spaces secret key (for TF state)           |

4. Go to **Actions** → **Deploy IBKR Relay** → **Run workflow** → select `deploy`

## Configuration

All configuration is via environment variables in `.env`:

| Variable                | Required | Default            | Description                                                          |
| ----------------------- | -------- | ------------------ | -------------------------------------------------------------------- |
| `DO_API_TOKEN`          | Yes      | —                  | DigitalOcean API token                                               |
| `TWS_USERID`            | Yes      | —                  | IBKR account username                                                |
| `TWS_PASSWORD`          | Yes      | —                  | IBKR account password                                                |
| `TRADING_MODE`          | No       | `paper`            | `paper` or `live`                                                    |
| `VNC_SERVER_PASSWORD`   | Yes      | —                  | Password for noVNC browser access                                    |
| `VNC_DOMAIN`            | Yes      | —                  | Domain for VNC access (see [Domains & HTTPS](#domains--https))       |
| `SITE_DOMAIN`           | Yes      | —                  | Domain for trade API (see [Domains & HTTPS](#domains--https))        |
| `API_TOKEN`             | Yes      | —                  | Bearer token for `/ibkr/*` endpoints (`openssl rand -hex 32`)        |
| `IBKR_FLEX_TOKEN`       | Yes      | —                  | Flex Web Service token (from Client Portal)                          |
| `IBKR_FLEX_QUERY_ID`    | Yes      | —                  | Flex Query ID (Trade Confirmation or Activity)                       |
| `TARGET_WEBHOOK_URL`    | No       | —                  | Webhook endpoint (empty = log-only dry-run)                          |
| `WEBHOOK_SECRET`        | No       | —                  | HMAC-SHA256 key for signing payloads (required if NOTIFIERS=webhook) |
| `NOTIFIERS`             | No       | —                  | Active notification backends (e.g. `webhook`). Empty = dry-run       |
| `POLL_INTERVAL_SECONDS` | No       | `600`              | Flex poll interval (seconds)                                         |
| `TIME_ZONE`             | No       | `America/New_York` | Timezone (tz database format)                                        |

## Webhook Payload

When orders fill, the relay POSTs a JSON payload with all trades batched into a single request:

```json
{
  "trades": [
    {
      "accountId": "UXXXXXXX",
      "symbol": "AAPL",
      "underlyingSymbol": "AAPL",
      "assetCategory": "STK",
      "listingExchange": "NASDAQ",
      "exchange": "IBDARK",
      "buySell": "BUY",
      "quantity": 1.0,
      "price": 254.6,
      "tradeDate": "20260402",
      "dateTime": "20260402;093008",
      "orderTime": "20260401;183713",
      "orderId": "684196618",
      "transactionId": "10101388829",
      "orderType": "MKT",
      "commission": -1.0,
      "commissionCurrency": "USD",
      "currency": "USD",
      "execIds": ["10101388829"],
      "fillCount": 1
    }
  ],
  "errors": []
}
```

The `trades` array contains one `Trade` object per order (fills are aggregated by `orderId`). The `errors` array contains warnings about unknown XML attributes or parse errors — it is empty when everything parsed cleanly. See [Flex XML Parsing](#flex-xml-parsing) for details.

Each `Trade` includes **all fields** from the IBKR Flex XML (see [`services/poller/models_poller.py`](services/poller/models_poller.py) for the full list). Most fields not present in the XML default to `""` or `0.0`, but `buySell` must be present; rows missing it are skipped and reported in `errors`.

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

## Commands

All operations are available via `make` or the Python CLI directly. Run `make help` to see the full list:

```
  make deploy      Deploy infrastructure (Terraform + Docker)
  make destroy     Permanently destroy all infrastructure
  make pause       Snapshot droplet + delete (save costs)
  make resume      Restore droplet from snapshot
  make sync        Push .env + restart (S=gateway B=1 LOCAL_FILES=1)
  make order       Place a stock order (e.g. make order Q=2 SYM=TSLA T=MKT [P=] [CUR=EUR] [EX=LSE] [TIF=GTC] [RTH=1])
  make poll        Trigger an immediate Flex poll (V=1 verbose, DEBUG=1 XML, REPLAY=N resend)
  make test-webhook Send sample trades to webhook endpoint
  make test         Run unit tests (pytest)
  make typecheck    Run mypy strict type checking
  make e2e          Run E2E tests against local paper account
  make e2e-up       Start E2E test stack (IB Gateway + remote-client)
  make e2e-run      Run E2E tests (stack must be up)
  make e2e-down     Stop and remove E2E test stack
  make local-up    Start full stack locally (no TLS, direct port access)
  make local-down  Stop local stack
  make gateway     Start IB Gateway container (then open VNC for 2FA)
  make logs        Stream poller logs (Ctrl+C to stop)
  make stats       Show container resource usage
  make ssh         SSH into the droplet
```

You can also invoke the CLI directly with `python3 -m cli <command>` — useful on Windows or when Make is not available:

> [!NOTE]
> Most commands work on Windows, but `sync --local-files` requires `rsync` and SSH, which are only available natively on macOS and Linux. On Windows, use [WSL](https://learn.microsoft.com/en-us/windows/wsl/).

```bash
python3 -m cli deploy
python3 -m cli sync gateway
python3 -m cli order 2 TSLA MKT
python3 -m cli order -2 TSLA LMT 380
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
make sync S=gateway                            # push .env + restart one service
make sync B=1                                  # push .env + rebuild images + restart
make sync LOCAL_FILES=1                        # rsync files + rebuild + restart (full deploy)
make sync LOCAL_FILES=1 S=poller               # full deploy, rebuild only poller
make order Q=2 SYM=TSLA T=MKT                  # buy 2 TSLA at market
make order Q=-2 SYM=TSLA T=LMT P=380           # sell 2 TSLA limit $380
make order Q=10 SYM=CSPX T=LMT P=590 CUR=EUR   # buy European ETF in EUR
make poll                                      # trigger immediate Flex poll
make poll V=1                                  # verbose (SSH, full poller logs)
make poll DEBUG=1                              # dump raw Flex XML
make poll REPLAY=3                             # resend 3 trades (skip dedup)
make test-webhook                              # send 3 sample trades to webhook
make test-webhook S=2                          # send to second webhook
make test                                      # run unit tests
make typecheck                                 # strict mypy checking
make logs                                      # stream poller logs
make logs S=remote-client                      # stream relay logs
make logs S=ib-gateway                         # stream gateway logs
make gateway                                   # start gateway + complete 2FA in browser
make pause                           # snapshot + delete droplet
make resume                          # restore from snapshot
```

### Which service to sync

After changing a variable in `.env`, restart only the affected service:

| Variable                                                                                                                              | Service       | Command               |
| ------------------------------------------------------------------------------------------------------------------------------------- | ------------- | --------------------- |
| `TWS_USERID`, `TWS_PASSWORD`, `TRADING_MODE`, `JAVA_HEAP_SIZE`                                                                        | ib-gateway    | `make sync S=gateway` |
| `API_TOKEN`                                                                                                                           | remote-client | `make sync S=relay`   |
| `IBKR_FLEX_TOKEN`, `IBKR_FLEX_QUERY_ID`, `TARGET_WEBHOOK_URL`, `WEBHOOK_SECRET`, `WEBHOOK_HEADER_NAME/VALUE`, `POLL_INTERVAL_SECONDS` | poller        | `make sync S=poller`  |
| `VNC_DOMAIN`, `SITE_DOMAIN`                                                                                                           | caddy         | `make sync S=caddy`   |
| Multiple services or unsure                                                                                                           | all           | `make sync`           |

### Syncing code changes

`make sync` only pushes `.env` and restarts containers — it does **not** update source files on the droplet. When you change Python code, Dockerfiles, or Compose config, use `LOCAL_FILES=1` to sync everything:

```bash
make sync LOCAL_FILES=1
```

This runs a full pre-deploy pipeline before anything reaches the droplet:

1. Verify you're on `main` (aborts on feature branches)
2. Verify working tree is clean (aborts on uncommitted changes)
3. `make typecheck` — mypy strict type checking
4. `make test` — all unit tests
5. `make e2e-run` — E2E tests (requires `make e2e-up` first)
6. `rsync` project files to the droplet (respects `.gitignore`, excludes `.env`)
7. Push `.env`
8. `docker compose up -d --build --force-recreate`

If any step fails, the deploy aborts — nothing reaches the droplet.

To skip E2E tests (e.g. docs-only changes):

```bash
make sync LOCAL_FILES=1 SKIP_E2E=1
```

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
│ ├── order.py # Place orders via HTTPS API
│ ├── poll.py # Trigger an immediate Flex poll
│ └── core/ # Project-agnostic (reusable across projects)
│   ├── __init__.py # CoreConfig dataclass, generic helpers (env, SSH, DO API, Terraform)
│   ├── deploy.py # Standalone (Terraform + rsync) or shared (rsync + compose)
│   ├── destroy.py # Terraform destroy
│   ├── pause.py # Snapshot + delete droplet
│   ├── resume.py # Restore from snapshot
│   └── sync.py # rsync files + pre-deploy checks + restart containers
├── .env.example # Configuration template
├── .github/workflows/
│ └── ci.yml # GitHub Actions: lint → typecheck → test
├── terraform/
│ ├── main.tf # Droplet, firewall, reserved IP, SSH key
│ ├── variables.tf # Terraform variables (infrastructure only)
│ ├── outputs.tf # Droplet IP, VNC/Site URLs, SSH key
│ └── cloud-init.sh # Docker install + creates project directory
├── docker-compose.yml # Container orchestration (6 services)
├── docker-compose.shared.yml # Shared-mode overlay (disables Caddy, uses relay-net)
├── docker-compose.local.yml # Local dev override (direct port access, no TLS)
├── docker-compose.test.yml # E2E test stack (ib-gateway + remote-client)
├── services/                  # Business-logic services (user-facing features)
│   ├── remote-client/
│   │   ├── Dockerfile
│   │   ├── requirements.txt       # ib_async, aiohttp
│   │   ├── main.py                # Entrypoint (connection + HTTP server)
│   │   ├── models_remote_client.py # Pydantic models (order API types)
│   │   ├── client/                # IB Gateway client (namespace delegation)
│   │   │   ├── __init__.py        # IBClient class (connection management)
│   │   │   ├── orders.py          # OrdersNamespace (place orders)
│   │   │   └── listener.py        # ListenerNamespace (real-time trade events → webhooks)
│   │   ├── routes/                # HTTP route handlers
│   │   │   ├── __init__.py        # Route orchestrator (create_routes)
│   │   │   ├── middlewares.py     # Auth middleware (Bearer token)
│   │   │   ├── order_place.py     # POST /ibkr/order
│   │   │   └── health.py          # GET /health
│   │   └── tests/e2e/             # E2E tests (paper account)
│   │       ├── conftest.py        # httpx fixtures
│   │       ├── .env.test.example  # Template for paper credentials
│   │       └── .env.test          # Your paper credentials (gitignored)
│   ├── poller/
│       ├── Dockerfile
│       ├── requirements.txt       # httpx, pydantic, aiohttp
│       ├── main.py                # Entrypoint (polling loop + HTTP API)
│       ├── models_poller.py       # Pydantic models (Fill, Trade, WebhookPayload, BuySell)
│       ├── poller/                # Core polling logic (package)
│       │   ├── __init__.py        # SQLite dedup, Flex fetch, poll_once()
│       │   ├── flex_parser.py     # Flex XML parser (Activity + Trade Confirmation)
│       │   ├── test_flex_parser.py # Tests for flex_parser
│       │   └── test_poller.py     # Tests for poller core logic
│       └── routes/                # HTTP API
│           ├── __init__.py        # Route orchestrator (create_routes, start_api_server)
│           ├── middlewares.py     # Auth middleware (Bearer token)
│           └── run.py             # POST /ibkr/poller/run handler
│   └── notifier/                  # Pluggable notification backends (library, no container)
│       ├── __init__.py            # Registry, load_notifiers(), validate_notifier_env(), notify()
│       ├── base.py                # BaseNotifier ABC
│       └── webhook.py             # WebhookNotifier: HMAC-SHA256 signed HTTP POST
├── infra/                         # Infrastructure backbone (no business logic)
│   ├── caddy/
│   │   ├── Caddyfile              # Reverse proxy config (VNC + Site domains)
│   │   ├── sites/
│   │   │   └── ibkr.caddy         # SITE_DOMAIN route handlers (handle /ibkr/*)
│   │   └── domains/
│   │       └── ibkr-vnc.caddy     # VNC_DOMAIN site block
│   ├── novnc/
│   │   └── index.html             # VNC web client (Start Gateway button)
│   └── gateway-controller/
│       ├── Dockerfile
│       ├── start-gateway.sh       # CGI script to start ib-gateway
│       └── gateway-status.sh      # CGI script to check ib-gateway status
└── types/                     # @tradegist/ibkr-types npm package
    ├── index.d.ts             # Barrel: exports IbkrPoller, IbkrHttp namespaces
    ├── package.json
    ├── poller/                # IbkrPoller namespace
    │   ├── index.d.ts
    │   └── types.d.ts         # Generated from services/poller/models_poller.py
    └── http/                  # IbkrHttp namespace
        ├── index.d.ts
        └── types.d.ts         # Generated from services/remote-client/models_remote_client.py

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

## Gateway Management

The IB Gateway session stays alive for approximately **one week** before IBKR forces re-authentication (2FA). You can safely **close the gateway** when you're not actively using the API — this lets you log in to the [IBKR Client Portal](https://portal.interactivebrokers.com) or mobile app normally (IBKR only allows one active session per user).

When you need the API again, restart the gateway using either method:

**From the command line:**

```bash
make gateway    # starts the container, then open vnc.example.com for 2FA
```

**From the browser:**

1. Go to `vnc.example.com`
2. The page detects the gateway is down and shows a **password field + Start Gateway** button
3. Enter the VNC password and click **Start Gateway**
4. The gateway starts and the VNC session connects automatically — complete 2FA when prompted

If 2FA times out before you complete it, the gateway exits cleanly and stops (it will **not** restart in a loop). Use either method above to try again.

## Placing Orders

Place stock orders from your local machine. The CLI is a convenience wrapper for `POST /ibkr/order` — it only supports `secType: STK` (stocks/ETFs). For other security types, call the API directly.

```bash
# Buy 2 shares of TSLA at market
python3 -m cli order 2 TSLA MKT

# Sell 2 shares of TSLA at market
python3 -m cli order -- -2 TSLA MKT

# Buy 2 shares of TSLA with a limit at $352.50
python3 -m cli order 2 TSLA LMT 352.5

# Sell 2 shares of TSLA with a limit at $380
python3 -m cli order -- -2 TSLA LMT 380

# Buy a European ETF in EUR
python3 -m cli order 10 CSPX LMT 590 EUR

# Buy on a specific exchange
python3 -m cli order 10 CSPX LMT 590 EUR LSE

# Good-til-cancelled order
python3 -m cli order 2 TSLA LMT 300 --tif GTC

# Allow execution outside regular trading hours
python3 -m cli order 2 TSLA LMT 300 --outside-rth
```

Or via `make`:

```bash
make order Q=2 SYM=TSLA T=MKT
make order Q=-2 SYM=TSLA T=LMT P=380
make order Q=10 SYM=CSPX T=LMT P=590 CUR=EUR
make order Q=10 SYM=CSPX T=LMT P=590 CUR=EUR EX=LSE
make order Q=2 SYM=TSLA T=LMT P=300 TIF=GTC
make order Q=2 SYM=TSLA T=LMT P=300 RTH=1
```

Positive quantity = **BUY**, negative = **SELL**. The script calls `https://<SITE_DOMAIN>/ibkr/order` over HTTPS with Bearer token authentication.

You can also call the API directly:

```bash
curl -X POST https://trade.example.com/ibkr/order \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <API_TOKEN>" \
  -d '{"contract": {"symbol": "TSLA"}, "order": {"action": "BUY", "totalQuantity": 2, "orderType": "MKT"}}'
```

For non-US stocks or ETFs, pass `exchange` and `currency` in the `contract` object (default: `SMART` and `USD`):

```bash
curl -X POST https://trade.example.com/ibkr/order \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <API_TOKEN>" \
  -d '{"contract": {"symbol": "CSPX", "exchange": "SMART", "currency": "EUR"}, "order": {"action": "BUY", "totalQuantity": 10, "orderType": "LMT", "lmtPrice": 590}}'
```

Example response:

```json
{
  "status": "PreSubmitted",
  "orderId": 8,
  "action": "BUY",
  "symbol": "TSLA",
  "totalQuantity": 2,
  "orderType": "MKT"
}
```

**Order API** field names mirror `ib_async` exactly (e.g. `lmtPrice`, `totalQuantity`, `secType`, `tif`, `outsideRth`). See [`services/remote-client/models_remote_client.py`](services/remote-client/models_remote_client.py) for the full schema.

> **Note**: The gateway must have `READ_ONLY_API=no` for orders to be accepted.

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

After resuming, you'll need to complete 2FA again via the VNC interface.

## SSH Access

```bash
make ssh
```

## Live Logs

Stream poller logs in real-time (useful for checking fill deliveries):

```bash
make logs
```

Stream remote client logs:

```bash
make logs S=remote-client
```

Stream IB Gateway logs:

```bash
make logs S=ib-gateway
```

## Security

- Firewall restricts SSH (22) and noVNC (6080) to the deployer's IP only
- IBKR API ports are Docker-internal — never exposed to the internet
- Webhook payloads are HMAC-SHA256 signed
- No credentials stored in the repository
- VNC requires a password

## Current Status

- [x] Terraform infrastructure (droplet, firewall, SSH key)
- [x] Docker Compose orchestration (6 containers)
- [x] Remote client connected to IB Gateway
- [x] HTTP API for order placement (US + international stocks/ETFs)
- [x] Flex poller with SQLite dedup + webhook delivery
- [x] On-demand poll endpoint (`make poll` / HTTP API)
- [x] Local deploy/destroy/pause/resume scripts
- [x] GitHub Actions workflow
- [x] Dry-run mode (log payloads when no webhook URL)
- [x] Webhook endpoint (HMAC-SHA256 signed, batched payloads)
- [x] Pluggable notification backends (`services/notifier/`, currently: webhook)
- [x] HTTPS via Caddy + Let's Encrypt (separate VNC/Trade domains)
- [x] Makefile CLI (`make deploy`, `make order`, etc.)
- [x] Gateway management (browser Start Gateway button + `make gateway`)
- [x] Unified Flex XML parsing (Activity + Trade Confirmation)
- [x] TypeScript type definitions (`@tradegist/ibkr-types`, not yet published)
- [x] E2E test infrastructure (Docker-based, paper account)
- [x] Real-time listener (opt-in, `LISTENER_ENABLED`)
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

- **All known fields are forwarded as-is** from the XML. The full list of supported fields is defined in [`services/poller/models_poller.py`](services/poller/models_poller.py). Unknown XML attributes are silently dropped but reported in the `errors` array of the webhook payload.

- **Fills are aggregated into trades** by `orderId`. When an order has multiple fills:
  - `quantity` is the sum of all fills
  - `price` is the quantity-weighted average
  - Financial fields (`commission`, `taxes`, `cost`, `tradeMoney`, `proceeds`, `netCash`, `fifoPnlRealized`, `mtmPnl`, `accruedInt`) are summed
  - `dateTime` and `tradeDate` use the latest value across fills
  - All other fields use the last fill's value
  - `execIds` is an array of `transactionId` values (one per fill), so you can trace back to individual executions
  - `fillCount` is the number of fills in the group

- **Deduplication** uses `transactionId` as the primary key (falling back to `ibExecId` → `tradeID`). Processed IDs are stored in SQLite to prevent double-sending across poll cycles.

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
| Transaction ID         | —              | `transactionId`    | —                       | Flex-only monotonic ID. Used as the primary dedup key in the poller.                                                             |
| Trade ID               | —              | `tradeID`          | —                       | Flex reporting grouping key. No real-time equivalent.                                                                            |
| Brokerage order ID     | —              | `brokerageOrderID` | —                       | IBKR internal routing ID.                                                                                                        |
| Exchange order ID      | —              | `exchOrderId`      | —                       | ID assigned by the exchange.                                                                                                     |
| External exec ID       | —              | `extExecID`        | —                       | Execution ID from the exchange.                                                                                                  |

**Cross-API join keys:**

- **Order level:** `permId` (TWS) ↔ `ibOrderID` (Flex AF) ↔ `orderID` (Flex TC)
- **Fill level:** `execId` (TWS) ↔ `ibExecID` (Flex AF) ↔ `execID` (Flex TC)

**This project's convention:** The permanent order ID (`permId` from ib_async) is exposed as `orderId` in all API responses (`PlaceOrderResponse`, `TradeDetail`). The session-scoped `orderId` from ib_async is never exposed — it resets on reconnect and is useless for cross-session tracking.

## Real-Time Listener

The listener is an **opt-in** feature that subscribes to IB Gateway trade events and fires webhooks immediately when orders fill — no polling delay.

### How it works

When enabled, the remote-client subscribes to two ib_async events:

- **`execDetailsEvent`** — fires when an execution occurs (fill price, quantity, exchange). Commission is not yet available.
- **`commissionReportEvent`** — fires shortly after with commission and realized P&L.

Each event produces a separate webhook with a single `Trade` object. The `source` field indicates the origin:

| Source                  | Commission | Latency        |
| ----------------------- | ---------- | -------------- |
| `execDetailsEvent`      | 0.0        | Instant        |
| `commissionReportEvent` | Populated  | ~0.5s after    |
| `flex`                  | Populated  | Minutes (poll) |

Both events fire for every fill — consumers receive two webhooks per fill and can choose which to act on (e.g. use `execDetailsEvent` for instant notification, `commissionReportEvent` for final commission data).

### Enable

Set `LISTENER_ENABLED` to any non-empty value in `.env` and configure at least one notifier backend:

```env
LISTENER_ENABLED=true
NOTIFIERS=webhook
TARGET_WEBHOOK_URL=https://example.com/webhook
WEBHOOK_SECRET=your_hmac_secret_key
```

The listener reuses the same notifier configuration as the poller (`NOTIFIERS`, `TARGET_WEBHOOK_URL`, `WEBHOOK_SECRET`, etc.).

### No cross-service dedup

The listener and poller fire independently. If the Gateway is connected and `LISTENER_ENABLED=true`, the same trade may produce webhooks from both the listener (real-time) and the poller (next poll cycle). Consumers should use the `source` field to distinguish or deduplicate by `ibExecId`.

### Field mapping

The listener maps ib_async event objects to the same `Trade` model used by the poller. Since the events provide a different set of fields than Flex XML, some Trade fields will be empty strings or `0.0` (e.g. `tradeDate`, `tradeMoney`, `proceeds`). The key fields (`symbol`, `buySell`, `quantity`, `price`, `orderId`, `ibExecId`, `commission`, `dateTime`) are always populated.
