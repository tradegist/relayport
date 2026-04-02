# IBKR Webhook Relay

Deploy a fully functional **Interactive Brokers Gateway API** to your own server with a few environment variables and one command.

## Why This Project?

IBKR has a notoriously difficult API. To automate anything — placing orders, getting fill confirmations — you need to run their Java-based Gateway or TWS application. That means either keeping it running on your local machine (impractical for web apps or any always-on service) or setting it up on a remote server (surprisingly painful to get right).

Thankfully, amazing open-source projects like [`ib_async`](https://github.com/ib-api-reloaded/ib_async), [`ib-gateway-docker`](https://github.com/gnzsnz/ib-gateway-docker), and others have made the core pieces much more accessible. This project wouldn't exist without them.

But even with those libraries, you still need to **build a Python app, deploy it somewhere, handle HTTPS, 2FA, reconnections, and webhooks**. That's where this project comes in — it bundles everything into a single `make deploy` that provisions a DigitalOcean droplet (starting at $12/month) with:

- **An HTTPS endpoint to place orders** via a simple REST API
- **A poller** that checks for trade fills every 10 minutes and sends them to your webhook URL

> **Only one endpoint for now?** Yes — more APIs will be exposed as the need arises. PRs welcome.

> **Why a poller instead of listening for events through the Gateway API?** Because IBKR only allows **one active session per user**. If the Gateway is connected and listening for fills, you can't use the IBKR Client Portal or mobile app at the same time. With the poller approach, you can **close the gateway** when you don't need programmatic order placement, trade normally via web/mobile, and know that ~10 minutes later the poller will detect and forward any fills to your webhook. The poller uses the [Flex Web Service](https://www.interactivebrokers.com/en/software/am/am/reports/activityflexqueries.htm) (a REST API), so it **does not require an active Gateway session**.

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
- [GitHub Actions](#github-actions-fork--deploy)
- [Project Structure](#project-structure)
- [Current Status](#current-status)

## API Endpoints

All endpoints require `Authorization: Bearer <API_TOKEN>` header.

#### Place an order

```
POST /ibkr/order
```

```json
{
  "quantity": 2,
  "symbol": "TSLA",
  "orderType": "MKT"
}
```

Optional fields: `limitPrice` (required for `LMT`), `currency` (default `USD`), `exchange` (default `SMART`).

#### Trigger a poll

```
POST /ibkr/run-poll
```

No body required. Immediately polls the Flex Web Service for new fills and sends them to the configured webhook.

#### Health check

```
GET /health
```

Returns `{"connected": true}` when the relay has an active connection to IB Gateway, `false` during reconnection (e.g. after a gateway restart). No auth required.

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
│  │  trade.example.com → webhook-relay:5000  │                │
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
- **`poller`** — Python image that polls the IBKR Flex Web Service every 10 minutes for trade confirmations and POSTs new fills to a webhook. Uses SQLite for deduplication. **Does not hold an IBKR session** — trade normally via web/mobile.
- **`gateway-controller`** — Lightweight Alpine sidecar with Docker CLI. Exposes a CGI endpoint so the noVNC page can start the gateway container from the browser.

## Domains & HTTPS

Two domain names are **required**. Caddy uses them to automatically provision TLS certificates from Let's Encrypt, providing secure HTTPS connections. Without valid domains, Caddy cannot obtain certificates and the services will not be accessible — there is no fallback to plain HTTP or IP-based access.

| Environment Variable | Purpose                                                      | Example             |
| -------------------- | ------------------------------------------------------------ | ------------------- |
| `VNC_DOMAIN`         | Serves the noVNC interface for IB Gateway 2FA authentication | `vnc.example.com`   |
| `TRADE_DOMAIN`       | Serves the order placement API (`/ibkr/order`, `/health`)    | `trade.example.com` |

### Setup

1. Point **both** domains to the droplet's reserved IP as **A records**:
   ```
   vnc.example.com    A    181.66.270.412
   trade.example.com  A    181.66.270.412
   ```
2. Set both in `.env`:
   ```
   VNC_DOMAIN=vnc.example.com
   TRADE_DOMAIN=trade.example.com
   ```
3. Start the stack — Caddy will automatically obtain and renew certificates for both domains.

> **Why two domains?** The VNC interface provides direct access to IB Gateway for 2FA and manual management. The trade API is a separate concern with its own authentication (Bearer token). Separating them on different domains provides clean isolation — you can restrict VNC access at the DNS/firewall level without affecting the trade API, and vice versa.

> **Can I use just an IP address?** No. Let's Encrypt does not issue certificates for bare IP addresses. The Caddy reverse proxy requires valid domain names to provision TLS certificates. Both `VNC_DOMAIN` and `TRADE_DOMAIN` must be set or the stack will refuse to start.

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
| `TRADE_DOMAIN`          | Domain for trade API                          |
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

| Variable                | Required | Default            | Description                                                    |
| ----------------------- | -------- | ------------------ | -------------------------------------------------------------- |
| `DO_API_TOKEN`          | Yes      | —                  | DigitalOcean API token                                         |
| `TWS_USERID`            | Yes      | —                  | IBKR account username                                          |
| `TWS_PASSWORD`          | Yes      | —                  | IBKR account password                                          |
| `TRADING_MODE`          | No       | `paper`            | `paper` or `live`                                              |
| `VNC_SERVER_PASSWORD`   | Yes      | —                  | Password for noVNC browser access                              |
| `VNC_DOMAIN`            | Yes      | —                  | Domain for VNC access (see [Domains & HTTPS](#domains--https)) |
| `TRADE_DOMAIN`          | Yes      | —                  | Domain for trade API (see [Domains & HTTPS](#domains--https))  |
| `API_TOKEN`             | Yes      | —                  | Bearer token for `/ibkr/*` endpoints (`openssl rand -hex 32`)  |
| `IBKR_FLEX_TOKEN`       | Yes      | —                  | Flex Web Service token (from Client Portal)                    |
| `IBKR_FLEX_QUERY_ID`    | Yes      | —                  | Trade Confirmation Flex Query ID                               |
| `TARGET_WEBHOOK_URL`    | No       | —                  | Webhook endpoint (empty = log-only dry-run)                    |
| `WEBHOOK_SECRET`        | Yes      | —                  | HMAC-SHA256 key for signing payloads                           |
| `POLL_INTERVAL_SECONDS` | No       | `600`              | Flex poll interval (seconds)                                   |
| `TIME_ZONE`             | No       | `America/New_York` | Timezone (tz database format)                                  |

## Webhook Payload

When an order fills, the relay POSTs a JSON payload:

```json
{
  "event": "fill",
  "symbol": "AAPL",
  "underlyingSymbol": "AAPL",
  "secType": "STK",
  "exchange": "NYSE",
  "op": "BUY",
  "quantity": 100.0,
  "avgPrice": 178.52,
  "tradeDate": "2026-04-01",
  "lastFillTime": "2026-04-01T14:30:00",
  "orderTime": "2026-04-01T09:31:05",
  "orderId": "1116304421",
  "execIds": ["5663526621", "5663526623"],
  "account": "UXXXXXXX",
  "commission": -1.0,
  "commissionCurrency": "USD",
  "currency": "USD",
  "orderType": "LMT",
  "fillCount": 2
}
```

The payload is signed with HMAC-SHA256. Verify using the `X-Signature-256` header:

```python
import hashlib, hmac

expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
assert header_value == f"sha256={expected}"
```

If `TARGET_WEBHOOK_URL` is empty, the relay logs the payload to stdout (dry-run mode) instead of sending it.

## Commands

All operations are available via `make` or the Python CLI directly. Run `make help` to see the full list:

```
  make deploy      Deploy infrastructure (Terraform + Docker)
  make destroy     Permanently destroy all infrastructure
  make pause       Snapshot droplet + delete (save costs)
  make resume      Restore droplet from snapshot
  make sync        Push .env + restart all services (or: make sync S=gateway)
  make order       Place an order (e.g. make order Q=2 SYM=TSLA T=MKT [P=] [CUR=EUR] [EX=LSE])
  make poll        Trigger an immediate Flex poll
  make gateway     Start IB Gateway container (then open VNC for 2FA)
  make logs        Stream poller logs (Ctrl+C to stop)
  make stats       Show container resource usage
  make ssh         SSH into the droplet
```

You can also invoke the CLI directly with `python3 -m cli <command>` — useful on Windows or when Make is not available:

```bash
python3 -m cli deploy
python3 -m cli sync gateway
python3 -m cli order 2 TSLA MKT
python3 -m cli order -2 TSLA LMT 380
python3 -m cli poll
python3 -m cli poll 2 # second poller
python3 -m cli pause
python3 -m cli resume
python3 -m cli destroy
```

`make` examples:

```bash
make deploy                                    # provision droplet + start containers
make sync S=gateway                            # update IBKR credentials on the droplet
make order Q=2 SYM=TSLA T=MKT                  # buy 2 TSLA at market
make order Q=-2 SYM=TSLA T=LMT P=380           # sell 2 TSLA limit $380
make order Q=10 SYM=CSPX T=LMT P=590 CUR=EUR   # buy European ETF in EUR
make poll                                      # trigger immediate Flex poll
make logs                                      # stream poller logs
make logs S=webhook-relay                      # stream relay logs
make gateway                                   # start gateway + complete 2FA in browser
make pause                           # snapshot + delete droplet
make resume                          # restore from snapshot
```

## Project Structure

```
├── Makefile               # CLI shortcuts (make deploy, make sync, etc.)
├── cli/                   # Python CLI (replaces shell scripts)
│   ├── __init__.py        # Shared helpers (env loading, SSH, DO API, validation)
│   ├── __main__.py        # Entry point (python3 -m cli <command>)
│   ├── deploy.py          # Terraform init + apply
│   ├── destroy.py         # Terraform destroy
│   ├── pause.py           # Snapshot + delete droplet
│   ├── resume.py          # Restore from snapshot
│   ├── sync.py            # Push .env + restart services
│   ├── order.py           # Place orders via HTTPS API
│   └── poll.py            # Trigger an immediate Flex poll
├── .env.example           # Configuration template
├── .github/workflows/
│   └── deploy.yml         # GitHub Actions workflow
├── terraform/
│   ├── main.tf            # Droplet, firewall, reserved IP, provisioners
│   ├── variables.tf       # Terraform variables
│   ├── outputs.tf         # Droplet IP, VNC/Trade URLs, SSH key
│   ├── cloud-init.sh      # Docker install + repo clone (no secrets)
│   └── env.tftpl          # .env template for file provisioner
├── docker-compose.yml     # Container orchestration (6 services)
├── caddy/
│   └── Caddyfile          # Reverse proxy config (VNC + Trade domains)
├── novnc/
│   └── index.html         # VNC web client (Start Gateway button)
├── gateway-controller/
│   ├── Dockerfile
│   ├── start-gateway.sh   # CGI script to start ib-gateway
│   └── gateway-status.sh  # CGI script to check ib-gateway status
├── remote-client/
│   ├── Dockerfile
│   ├── requirements.txt   # ib_async, aiohttp
│   └── client.py          # IB Gateway client + authenticated order API
└── poller/
    ├── Dockerfile
    ├── requirements.txt   # httpx
    └── poller.py          # Flex trade poller + webhook sender
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

Place stock orders from your local machine:

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
```

Or via `make`:

```bash
make order Q=2 SYM=TSLA T=MKT
make order Q=-2 SYM=TSLA T=LMT P=380
make order Q=10 SYM=CSPX T=LMT P=590 CUR=EUR
make order Q=10 SYM=CSPX T=LMT P=590 CUR=EUR EX=LSE
```

Positive quantity = **BUY**, negative = **SELL**. The script calls `https://<TRADE_DOMAIN>/ibkr/order` over HTTPS with Bearer token authentication.

You can also call the API directly:

```bash
curl -X POST https://trade.example.com/ibkr/order \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <API_TOKEN>" \
  -d '{"quantity": 2, "symbol": "TSLA", "orderType": "MKT"}'
```

For non-US stocks or ETFs, pass `exchange` and `currency` (default: `SMART` and `USD`):

```bash
curl -X POST https://trade.example.com/ibkr/order \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <API_TOKEN>" \
  -d '{"quantity": 10, "symbol": "CSPX", "orderType": "LMT", "limitPrice": 590, "exchange": "SMART", "currency": "EUR"}'
```

Example response:

```json
{
  "status": "PreSubmitted",
  "orderId": 8,
  "action": "BUY",
  "symbol": "TSLA",
  "quantity": 1,
  "orderType": "MKT"
}
```

| Field        | Required | Default | Description                           |
| ------------ | -------- | ------- | ------------------------------------- |
| `quantity`   | Yes      | —       | Positive = BUY, negative = SELL       |
| `symbol`     | Yes      | —       | Ticker symbol                         |
| `orderType`  | Yes      | —       | `MKT` or `LMT`                        |
| `limitPrice` | LMT only | —       | Limit price                           |
| `exchange`   | No       | `SMART` | Exchange (SMART routes automatically) |
| `currency`   | No       | `USD`   | Trading currency (EUR, GBP, etc.)     |

> **Note**: The gateway must have `READ_ONLY_API=no` for orders to be accepted.

## On-Demand Poll

Trigger an immediate poll without waiting for the next interval:

```bash
make poll
```

Or call the endpoint directly with `curl`:

```bash
source .env && curl -s -X POST "https://${TRADE_DOMAIN}/ibkr/run-poll" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  | python3 -m json.tool
```

You can optionally override the Flex token and query ID in the request body (defaults to the env vars if omitted):

```bash
curl -s -X POST "https://trade.example.com/ibkr/run-poll" \
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
make logs S=webhook-relay
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
- [x] HTTPS via Caddy + Let's Encrypt (separate VNC/Trade domains)
- [x] Makefile CLI (`make deploy`, `make order`, etc.)
- [x] Gateway management (browser Start Gateway button + `make gateway`)
- [ ] Health monitoring / alerting
