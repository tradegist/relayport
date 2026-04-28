# RelayPort

A **relay between broker accounts** that provides clear, common interfaces to communicate with different brokers through a single interface layer — deployed to a DigitalOcean droplet with a single `make deploy`.

> [!WARNING]
> This project is under active development and not yet ready for prime time. You're welcome to use it, but expect frequent breaking changes.

## Why This Project?

Broker APIs are fragmented — each has its own data formats, auth patterns, and delivery mechanisms. Every integration rebuilds the same plumbing: polling, parsing, dedup, webhook delivery.

RelayPort abstracts this with a **relay adapter pattern**: one generic engine handles polling, dedup, aggregation, and webhook delivery; broker-specific adapters handle the API quirks. Adding a broker is writing one adapter.

Currently supports **IBKR** (Interactive Brokers) via the Flex Web Service and **Kraken** (crypto exchange) via REST + WebSocket v2. Deploys to a DigitalOcean droplet from **$4/month**, with:

- **A relay engine** that checks for trade fills and sends them to your webhook URL via a common payload format
- **Automatic HTTPS** via Caddy + Let's Encrypt
- **SQLite dedup** so each fill is delivered exactly once
- **A debug webhook inbox** for testing without hitting production services
- **Multi-account support** within each broker adapter
- **Optional real-time listeners** — IBKR via [ibkr_bridge](https://github.com/tradegist/ibkr_bridge) WebSocket, Kraken via native WS v2 executions channel

**Scope:** Broker → User (trade fill events). Future plans include User → Broker (order placement).

> **For IBKR order placement**, see the companion project [ibkr_bridge](https://github.com/tradegist/ibkr_bridge) — it runs the IB Gateway and exposes an HTTP API + WebSocket event stream.

## Table of Contents

- [Quick Start](#quick-start)
- [API Endpoints](#api-endpoints)
- [Architecture](#architecture)
- [Configuration](#configuration)
- [Webhook Payload](#webhook-payload)
  - [Option contracts](#option-contracts)
  - [FX Rate Enrichment](#fx-rate-enrichment)
  - [Debug Webhook Inbox](#debug-webhook-inbox)
- [IBKR Setup](#ibkr-setup)
- [Kraken Setup](#kraken-setup)
- [On-Demand Poll](#on-demand-poll)
- [Pause & Resume](#pause--resume)
- [Security](#security)
- [Current Status](#current-status)
- [Contributing](#contributing)

## Quick Start

A fully-fledged IBKR relay server on DigitalOcean in under 2 minutes:

```
git clone  →  make setup  →  set env vars  →  make deploy  →  trade fills hit your webhook
```

### Prerequisites

- [Docker Desktop](https://docs.docker.com/desktop/) (includes Docker Compose v2)
- [Terraform](https://developer.hashicorp.com/terraform/install)
- A [DigitalOcean API token](https://cloud.digitalocean.com/account/api/tokens)
- An IBKR account with Flex Web Service enabled (see [IBKR Setup](#ibkr-setup))

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

### Trigger a poll

```
POST /relays/{relay_name}/poll/{poll_idx}
```

No body required. Immediately polls the broker for new fills and sends them to the configured webhook. `poll_idx` is 1-based (e.g. `/relays/ibkr/poll/1` for the primary poller, `/relays/ibkr/poll/2` for the second account).

### Health check

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

## Configuration

Configuration is split across three environment files. Templates are in `env_examples/` — `make setup` copies them to `.<name>` if missing.

### `.env` — App config

| Variable                            | Required | Default | Description                                                                                                                    |
| ----------------------------------- | -------- | ------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `SITE_DOMAIN`                       | Yes      | —       | Domain for the relay API (see [Domains & HTTPS](#domains--https))                                                              |
| `API_TOKEN`                         | Yes      | —       | Bearer token for `/relays/*` endpoints (`openssl rand -hex 32`)                                                                |
| `RELAYS`                            | No       | —       | Comma-separated relay adapters (e.g. `ibkr`, `ibkr,kraken`). Empty = API server only                                           |
| `NOTIFIERS`                         | No       | —       | Active notification backends (e.g. `webhook`). Empty = dry-run                                                                 |
| `TARGET_WEBHOOK_URL`                | No       | —       | Webhook endpoint (empty = log-only dry-run)                                                                                    |
| `WEBHOOK_SECRET`                    | No       | —       | HMAC-SHA256 key for signing payloads (required if NOTIFIERS=webhook)                                                           |
| `POLL_INTERVAL`                     | No       | `600`   | Flex poll interval (seconds). **IBKR limit: 10 req/minute per token (shared across query IDs) — do not set below 420 (7 min)** |
| `POLLER_ENABLED`                    | No       | `true`  | Set to `false` to disable the poller globally (relay override: `{RELAY}_POLLER_ENABLED`)                                       |
| `LISTENER_ENABLED`                  | No       | —       | Set to `true` to enable real-time WS listeners globally; IBKR requires `ibkr_bridge`, Kraken does not                          |
| `LISTENER_DEBOUNCE_MS`              | No       | `0`     | Milliseconds to buffer fills before flushing                                                                                   |
| `IBKR_LISTENER_EXEC_EVENTS_ENABLED` | No       | `false` | Enable `execDetailsEvent` webhooks (2x volume, lower latency)                                                                  |
| `DEBUG_WEBHOOK_PATH`                | No       | —       | Route webhooks to debug inbox instead of `TARGET_WEBHOOK_URL` (see [Debug Webhook Inbox](#debug-webhook-inbox))                |
| `MAX_DEBUG_WEBHOOK_PAYLOADS`        | No       | `100`   | Max payloads stored in the debug inbox (hard max: 150, FIFO eviction)                                                          |
| `DEBUG_LOG_LEVEL`                   | No       | `INFO`  | Set to `DEBUG` to include full payload+headers in `docker logs debug`                                                          |
| `FX_RATES_ENABLED`                  | No       | `false` | Attach `fxRate`/`fxRateBase`/`fxRateSource` to each Trade (see [FX Rate Enrichment](#fx-rate-enrichment))                      |
| `FX_RATES_BASE_CURRENCY`            | No\*     | —       | ISO-4217 base currency (required when `FX_RATES_ENABLED=true`)                                                                 |
| `FX_RATE_API_KEY`                   | No       | —       | [exchangerate-api.com](https://www.exchangerate-api.com) key — enables historical rates                                        |
| `FX_CACHE_RETENTION_DAYS`           | No       | `730`   | Days to retain cached historical rates in the meta DB                                                                          |

### `.env.droplet` — CLI-only (never pushed to containers)

| Variable       | Required | Default            | Description                                                                  |
| -------------- | -------- | ------------------ | ---------------------------------------------------------------------------- |
| `DEPLOY_MODE`  | Yes      | —                  | `standalone` (own droplet via Terraform) or `shared` (existing droplet)      |
| `DO_API_TOKEN` | Yes\*    | —                  | DigitalOcean API token (standalone only — can be removed after first deploy) |
| `DROPLET_IP`   | Yes\*    | —                  | Droplet IP (from Terraform output in standalone; provided by host in shared) |
| `SSH_KEY`      | No       | `~/.ssh/relayport` | SSH key path — **shared mode only**. Standalone auto-generates.              |
| `DROPLET_SIZE` | No       | `s-1vcpu-512mb`    | Override droplet size slug                                                   |

### `.env.relays` — Relay-prefixed vars

| Variable                       | Required | Description                                                                                                                 |
| ------------------------------ | -------- | --------------------------------------------------------------------------------------------------------------------------- |
| `IBKR_FLEX_TOKEN`              | Yes      | Flex Web Service token (from Client Portal)                                                                                 |
| `IBKR_FLEX_QUERY_ID`           | Yes      | Flex Query ID (Trade Confirmation or Activity)                                                                              |
| `IBKR_ACCOUNT_TIMEZONE`        | No       | IANA tz for IBKR timestamps (e.g. `America/New_York`). Default: `UTC`. Invalid value fails boot                             |
| `IBKR_FLEX_QUERY_ID_2`         | No       | Second account query ID (enables second poller within same relay)                                                           |
| `IBKR_FLEX_TOKEN_2`            | No       | Second account token (defaults to primary if omitted)                                                                       |
| `IBKR_NOTIFIERS`               | No       | Override `NOTIFIERS` for IBKR relay only                                                                                    |
| `IBKR_TARGET_WEBHOOK_URL`      | No       | Override `TARGET_WEBHOOK_URL` for IBKR relay only                                                                           |
| `IBKR_WEBHOOK_SECRET`          | No       | Override `WEBHOOK_SECRET` for IBKR relay only                                                                               |
| `IBKR_POLL_INTERVAL`           | No       | Override `POLL_INTERVAL` for IBKR relay only. **Minimum recommended: 420 (7 min) — see rate-limit note in `POLL_INTERVAL`** |
| `IBKR_POLLER_ENABLED`          | No       | Override `POLLER_ENABLED` for IBKR relay only                                                                               |
| **Kraken**                     |          |                                                                                                                             |
| `KRAKEN_API_KEY`               | Yes\*    | Kraken API key (required when `kraken` is in `RELAYS`)                                                                      |
| `KRAKEN_API_SECRET`            | Yes\*    | Kraken API secret, base64-encoded (required with API key)                                                                   |
| `KRAKEN_LISTENER_ENABLED`      | No       | Enable WS v2 real-time listener (default: `false`)                                                                          |
| `KRAKEN_LISTENER_DEBOUNCE_MS`  | No       | Buffer fills N ms before dispatching webhook (default: `0`)                                                                 |
| `KRAKEN_LOOKBACK_DAYS`         | No       | How far back each REST poll looks for trades, in days (default: `30`, min: `1`)                                             |
| `KRAKEN_POLL_INTERVAL`         | No       | Override `POLL_INTERVAL` for Kraken relay only                                                                              |
| `KRAKEN_POLLER_ENABLED`        | No       | Override `POLLER_ENABLED` for Kraken relay only                                                                             |
| `KRAKEN_NOTIFIERS`             | No       | Override `NOTIFIERS` for Kraken relay only                                                                                  |
| `KRAKEN_TARGET_WEBHOOK_URL`    | No       | Override `TARGET_WEBHOOK_URL` for Kraken relay only                                                                         |
| `KRAKEN_WEBHOOK_SECRET`        | No       | Override `WEBHOOK_SECRET` for Kraken relay only                                                                             |
| `KRAKEN_NOTIFY_RETRIES`        | No       | Override `NOTIFY_RETRIES` for Kraken relay only                                                                             |
| `KRAKEN_NOTIFY_RETRY_DELAY_MS` | No       | Override `NOTIFY_RETRY_DELAY_MS` for Kraken relay only                                                                      |

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
      "timestamp": "2026-04-02T13:30:08",
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

| Field          | Type                     | Description                                                                                                                                                            |
| -------------- | ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `orderId`      | `string`                 | Permanent order identifier (unique per account)                                                                                                                        |
| `symbol`       | `string`                 | Instrument symbol. For options, this is the OCC ticker with spaces removed for URL-friendliness (e.g. `AVGO260620C00200000`); the underlying is in `option.rootSymbol` |
| `assetClass`   | `AssetClass`             | `"equity"`, `"option"`, `"crypto"`, `"future"`, `"forex"`, or `"other"`                                                                                                |
| `side`         | `"buy" \| "sell"`        | Trade direction (lowercase)                                                                                                                                            |
| `orderType`    | `OrderType \| null`      | Normalized: `"market"`, `"limit"`, `"stop"`, `"stop_limit"`, `"trailing_stop"`, or `null`                                                                              |
| `price`        | `number`                 | VWAP when aggregated, single fill price otherwise                                                                                                                      |
| `volume`       | `number`                 | Sum of fill quantities                                                                                                                                                 |
| `cost`         | `number`                 | Total cost (sum of fills)                                                                                                                                              |
| `fee`          | `number`                 | Total fees/commissions (always positive — amount paid)                                                                                                                 |
| `fillCount`    | `number`                 | Number of fills aggregated into this trade                                                                                                                             |
| `execIds`      | `string[]`               | One execution ID per fill (for tracing back to individual fills)                                                                                                       |
| `timestamp`    | `string`                 | Latest fill timestamp. Canonical form: `YYYY-MM-DDTHH:MM:SS`, always UTC, no `Z` suffix, no fractional seconds                                                         |
| `source`       | `string`                 | Origin: `"flex"` (IBKR Flex poll), `"execDetailsEvent"` / `"commissionReportEvent"` (IBKR WS), `"rest_poll"` (Kraken REST), `"ws_execution"` (Kraken WS)               |
| `currency`     | `string \| null`         | ISO-4217 currency of the asset traded (e.g. `"USD"` for AAPL). `null` when the broker doesn't expose it                                                                |
| `option`       | `OptionContract \| null` | Option contract metadata. Populated when `assetClass == "option"`, `null` for all other instruments. See [Option contracts](#option-contracts) below                   |
| `fxRate`       | `number \| null`         | FX rate such that `cost * fxRate = cost_in_base`. Only populated when `FX_RATES_ENABLED=true` (see [FX Rate Enrichment](#fx-rate-enrichment))                          |
| `fxRateBase`   | `string \| null`         | ISO-4217 base currency the `fxRate` converts to                                                                                                                        |
| `fxRateSource` | `string \| null`         | `"historical"` or `"latest"` — whether the rate is the trade-day rate (paid API) or most recent (keyless)                                                              |
| `raw`          | `object`                 | Original broker-specific payload (all fields, unmodified)                                                                                                              |

The `raw` object preserves the full broker-specific data. For IBKR Flex, this includes ~100 XML attributes (account info, security details, financial fields, dates). Consumers should treat `raw` as opaque broker data — the CommonFill fields above are the stable contract.

The `errors` array contains warnings about parse problems — it is empty when everything parsed cleanly.

### Option contracts

When `assetClass == "option"`, the `option` object is populated (non-null) and contains:

| Field        | Type              | Description                              |
| ------------ | ----------------- | ---------------------------------------- |
| `rootSymbol` | `string`          | Underlying ticker (e.g. `"AVGO"`)        |
| `strike`     | `number`          | Strike price                             |
| `expiryDate` | `string`          | Expiry date in ISO format (`YYYY-MM-DD`) |
| `type`       | `"call" \| "put"` | Option type                              |

Example — IBKR option trade (AVGO call, sold via Flex):

```json
{
  "relay": "ibkr",
  "type": "trades",
  "data": [
    {
      "orderId": "684196620",
      "symbol": "AVGO260620C00200000",
      "assetClass": "option",
      "side": "sell",
      "orderType": "limit",
      "price": 5.2,
      "volume": 1.0,
      "cost": 520.0,
      "fee": 0.65,
      "fillCount": 1,
      "execIds": ["0001f4e8.67890abc.02.01"],
      "timestamp": "2026-04-02T14:05:00",
      "source": "flex",
      "currency": "USD",
      "option": {
        "rootSymbol": "AVGO",
        "strike": 200.0,
        "expiryDate": "2026-06-20",
        "type": "call"
      },
      "raw": { "...": "..." }
    }
  ],
  "errors": []
}
```

Rows with `assetClass == "option"` where option metadata is missing or invalid are skipped and surfaced in the `errors` array rather than emitted with an incomplete `option` object. This means any trade that reaches your webhook with `assetClass == "option"` is guaranteed to have a non-null `option` field — the invariant is enforced by the parsers rather than by the type schema (which models `option` as `OptionContract | null` to cover non-option assets).

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

### FX Rate Enrichment

Each outbound Trade can optionally include FX rate information so downstream systems can convert `cost` into a single reporting currency. Opt in via `.env`:

```env
FX_RATES_ENABLED=true
FX_RATES_BASE_CURRENCY=EUR
# Optional — enables historical rates for any trade date:
#FX_RATE_API_KEY=your-exchangerate-api-key
# Optional — retention for cached historical rates in the meta DB (default: 730):
#FX_CACHE_RETENTION_DAYS=730
```

**Convention:** `fxRate` is expressed as _units of `fxRateBase` per 1 unit of `currency`_, so `cost * fxRate = cost_in_base`. Example: for a USD trade with base EUR, `fxRate ≈ 0.835` (i.e. `1 USD → 0.835 EUR`).

**With an API key** ([exchangerate-api.com](https://www.exchangerate-api.com)) — the relay uses the `/history/{base}/{YYYY}/{M}/{D}` endpoint to fetch the trade-day rate. Rates are cached in-memory and persisted to the `relay-meta` Docker volume so restarts don't refetch.

**Without an API key** — the relay falls back to the keyless [open.er-api.com](https://open.er-api.com) latest endpoint (no history available). Trades older than today ship with `fxRate=null` and a human-readable reason appended to the payload's `errors` array:

```json
{
  "relay": "ibkr",
  "type": "trades",
  "data": [{ "orderId": "123", "currency": "USD", "fxRate": null, "fxRateBase": null, "fxRateSource": null, ... }],
  "errors": ["Trade 123: historical FX unavailable (trade date 2026-04-10 < today 2026-04-19; set FX_RATE_API_KEY to enable historical lookups) — fxRate omitted"]
}
```

**Currency detection** is per-relay:

- **IBKR** — lifted directly from the Flex XML `currency` attribute / bridge `contract.currency`.
- **Kraken** — resolved from the pair's quote side. Known stablecoins are normalised (`USDT`/`USDC`/`DAI`/`PYUSD`/`TUSD`/`FDUSD`/`USDP` → `USD`; `EURT`/`EURC` → `EUR`; `GBPT` → `GBP`). Crypto-quoted-in-crypto pairs (e.g. `ETH/BTC`) ship without `fxRate`.

Upstream failures, unknown currencies, and missing API keys are isolated per-trade: a single bad lookup never prevents a trade from shipping.

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

## IBKR Setup

Before deploying, create an Activity Flex Query in IBKR Client Portal:

1. Log in to [Client Portal](https://portal.interactivebrokers.com)
2. Go to **Reporting** → **Flex Queries**
3. Under **Activity Flex Query**, click **+** to create a new query
4. Set **Period** to **Last 7 Days** (covers missed fills if the droplet was down)
5. In **Sections**, enable **Trades** and select the execution fields you want
6. Set **Format** to **XML**
7. Set the **Date Format** to **`yyyyMMdd`**, the **Time Format** to **`HHmmss`**, and the **Date/Time Separator** to **`;` (semi-colon)** — these are the only values the parser supports. Any other combination will cause fill rows to be skipped with a timestamp parse error.
8. Save and note the **Query ID** (use as `IBKR_FLEX_QUERY_ID` in `.env.relays`)
9. Go to **Flex Web Service Configuration** → enable and get the **Current Token** (use as `IBKR_FLEX_TOKEN` in `.env.relays`)

### IBKR polling (Flex Web Service)

The IBKR poller calls the Flex Web Service at the configured interval (default: 600s). Override with `IBKR_POLL_INTERVAL` in `.env.relays`.

> **Rate limit:** IBKR enforces a limit of **10 requests per minute per token** (and 1 per second), per [Flex Web Service error code 1018](https://www.ibkrguides.com/orgportal/performanceandstatements/flex3error.htm). The limit is scoped to the _token_, so multiple query IDs (e.g. `_2` suffixed pollers) share the same budget. Hitting it returns `ErrorCode 1018 — Too many requests`. The technical floor is ~6 seconds, but Flex report generation is slow (5–30 s typical), retries need headroom, and there's no benefit to polling faster than the broker generates data. We recommend `IBKR_POLL_INTERVAL` (or `POLL_INTERVAL`) at **420 seconds (7 minutes) minimum**; the default of 600 s (10 min) is a comfortable margin.

> **Why Activity instead of Trade Confirmation?** Trade Confirmation queries are locked to "Today" only. Activity queries support a configurable lookback period, so if the droplet is offline for a few days the first poll after restart will catch all missed fills. The SQLite dedup prevents double-sending.

### IBKR real-time listener

The IBKR relay includes an optional real-time listener that subscribes to [ibkr_bridge](https://github.com/tradegist/ibkr_bridge)'s WebSocket event stream for near-instant fill delivery — complementing the Flex poller (which runs every 10 minutes by default).

> **Prerequisite:** A running [ibkr_bridge](https://github.com/tradegist/ibkr_bridge) instance is required. The listener authenticates via the bridge's `API_TOKEN`.

#### Enabling the listener

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

#### Event types

The listener processes two event types from the bridge stream:

| Event                   | Default     | Description                                                                                                                                                                                                       |
| ----------------------- | ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `commissionReportEvent` | **enabled** | Fired after commission is confirmed — contains the final fill with fee data. This is the primary fill event.                                                                                                      |
| `execDetailsEvent`      | disabled    | Fired immediately on execution — no commission data yet. Enable with `IBKR_LISTENER_EXEC_EVENTS_ENABLED=true` for sub-second latency at the cost of 2× webhook volume (one preliminary + one confirmed per fill). |

#### Operational notes

- **Dedup is shared with the Flex poller.** Both the listener and the Flex poller write to the same SQLite dedup database. A fill delivered by the listener will be silently skipped if the Flex poller later sees the same `execId`, and vice versa.
- **Auto-reconnect with backoff.** On disconnect or error the listener waits (starting at 5 s, up to 5 min) and reconnects automatically. The last seen sequence number is sent on reconnect so the bridge can replay any missed events.
- **Debounce (optional).** Set `LISTENER_DEBOUNCE_MS` (milliseconds, default `0`) to buffer rapid partial fills before dispatching a single batched webhook. Useful when a large order fills in many small lots within a short window.

#### Disabling the listener

Remove or comment out `LISTENER_ENABLED` (or set it to `false`) and run `make sync`. The listener task is not started on the next container restart.

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
# .env.relays
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
      "price": 2450.5,
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

Targets the droplet by default. Set `DEFAULT_CLI_ENV=local` in `.env.droplet` (or pass `ENV=local`) to stream from the local stack instead:

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
- [x] TypeScript type definitions (`@tradegist/relayport-types`, not yet published)
- [x] Python type definitions (`relayport-types`, not yet published)
- [x] Multi-account support within each relay (`_2` suffix)
- [x] Debug webhook inbox (`DEBUG_WEBHOOK_PATH`)
- [x] Real-time listener (ibkr_bridge WebSocket)
- [x] Env file split (`.env` + `.env.droplet` + `.env.relays`)
- [ ] Health monitoring / alerting
- [x] Kraken crypto exchange adapter (REST poller + WS v2 listener)
- [ ] Additional broker adapters

## Contributing

Developer and contributor documentation — testing, full commands reference, project structure, type regeneration, and broker-adapter internals (including the Flex XML parser and IBKR ID reference) — lives in [CONTRIBUTING.md](CONTRIBUTING.md).
