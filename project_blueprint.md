# Exchange Webhook Relay — Project Blueprint

> **Purpose:** This document is the technical specification for scaffolding a new
> exchange relay project (e.g. `binance_relay`, `kraken_relay`). The project
> listens to an exchange via WebSocket and REST API, detects completed trades,
> and fires webhook notifications. It is designed to be handed to an AI agent
> in a fresh repository with zero prior context.
>
> **To adapt:** Replace `kraken` with the target exchange name throughout.
> Replace API endpoints, auth methods, and WS message formats with the
> exchange's equivalents. The architecture, dedup, notifier, CLI, deployment,
> and TypeScript types are exchange-agnostic.

---

## 1. What This Project Does

Connect to **Kraken (Payward)** and notify an external webhook URL when orders
are filled. Two data sources:

| Service      | Source              | Trigger                                           |
| ------------ | ------------------- | ------------------------------------------------- |
| **listener** | Kraken WebSocket v2 | Real-time `executions` channel push               |
| **poller**   | Kraken REST API     | Periodic poll of `ClosedOrders` / `TradesHistory` |

Both services:

1. Receive trade/fill data from Kraken.
2. Deduplicate against a **shared** SQLite database (WAL mode, by execution ID).
3. Aggregate fills into trades.
4. POST a signed JSON webhook to `TARGET_WEBHOOK_URL`.

Both services share a single dedup database on a `dedup-data` Docker volume.
A fill processed by the listener is automatically skipped by the next poll
cycle, and vice versa.

The **listener** gives near-instant notifications. The **poller** is a
reliability fallback that catches anything the WebSocket missed (disconnects,
restarts, etc.).

---

## 2. Deployment Modes

This project can be deployed in two ways: on its own dedicated droplet
(standalone) or alongside other relay projects on an existing droplet (shared).

The mode is controlled by **`DEPLOY_MODE`** in `.env` — a required env var
validated before any deploy or sync. No implicit detection from token presence.

| Mode           | `DEPLOY_MODE=` | Requirements             | What `make deploy` Does                                                        |
| -------------- | -------------- | ------------------------ | ------------------------------------------------------------------------------ |
| **Standalone** | `standalone`   | `DO_API_TOKEN`           | Terraform creates droplet → CLI rsyncs files, pushes `.env`, runs `compose up` |
| **Shared**     | `shared`       | `DROPLET_IP` + `SSH_KEY` | rsync + `docker compose -f docker-compose.shared.yml up`                       |

`DO_API_TOKEN` can be removed from `.env` after the first standalone deploy
for security — the mode is determined by `DEPLOY_MODE`, not by token presence.

In shared mode, the project deploys alongside other relay projects
on the same droplet. Each project lives in its own directory
(`/opt/kraken-relay/`) and runs its own Docker Compose stack. A shared Caddy
instance routes traffic by URL prefix.

### Shared Infrastructure — Caddy Routing

When multiple relay projects coexist on one droplet, Caddy uses `import` with
file globbing to compose routes from all projects:

**Main Caddyfile** (owned by whichever project created the droplet):

```caddy
{$SITE_DOMAIN} {
    import /etc/caddy/sites/*.caddy
    import /etc/caddy/shared-sites/*.caddy
}

import /etc/caddy/domains/*.caddy
import /etc/caddy/shared-domains/*.caddy
```

**Each project provides snippet files:**

- `infra/caddy/sites/kraken.caddy` — route handlers (imported inside `{$SITE_DOMAIN}`)
- `infra/caddy/domains/` — full site blocks (if the project needs its own domain)

During shared deploy, snippet files are **templated** (all `{$VAR}` placeholders
replaced with literal values from `.env`) and copied to `/opt/caddy-shared/`
on the droplet. This avoids requiring the host Caddy to have the shared
project’s env vars.

Example `infra/caddy/sites/kraken.caddy`:

```caddy
handle /kraken/poller/* {
    reverse_proxy kraken-poller:8000
}

handle /kraken/* {
    reverse_proxy kraken-listener:5000
}
```

**Routing rules:**

- Every project's routes MUST be under its own prefix (`/kraken/*` for this project).
- At deploy time, `route_prefix` on `CoreConfig` (e.g. `"/kraken"`) is used to validate that every `handle` directive in `sites/*.caddy` starts with the prefix. Reject the deploy if any route violates this.
- The Caddy snippet is SCP'd to `/opt/caddy-shared/sites/<project_name>.caddy` on the droplet, then `docker exec caddy caddy reload`.

### Docker External Network

The base `docker-compose.yml` creates a named network `relay-net` (the
standalone project IS the host). In shared mode, the overlay marks it
`external: true` (the host project already created it).

**Base compose (`docker-compose.yml`):**

```yaml
networks:
  default:
    name: relay-net
```

**Shared overlay (`docker-compose.shared.yml`):**

```yaml
networks:
  default:
    name: relay-net
    external: true

services:
  kraken-listener:
    networks:
      - default
      - relay-net
```

---

## 3. Mandatory Conventions

These are non-negotiable. Every rule below applies from the first commit.

### 3.1 Code Quality

- **No unused imports.** After writing or editing any Python file, verify every `import` is used. Remove unused ones.
- **No `__all__`.** All imports are explicit (`from module import X`). `__all__` only controls star-imports, which we never use.
- **Makefile must mirror CLI arguments.** When adding a new parameter to a `cli/` command, always add the corresponding `$(if $(VAR),--flag $(VAR))` to the Makefile target so `make <target> VAR=value` works.
- **Update README.md when changing public interfaces.** When adding or modifying CLI commands, Makefile targets, API endpoints, or env vars, always update the README to reflect the change.
- **Run `make lint` after every code change.** Ruff is the linter. Fix all errors before committing. Use `make lint FIX=1` to auto-fix safe issues.
- **Run `make test` and `make typecheck` after every code change**, even refactors. Do not wait until the end — verify immediately.

### 3.2 Security

- **No hardcoded credentials** — API keys, secrets, passwords MUST come from environment variables (`.env`). Never write real values in source files.
- **No hardcoded IPs** — use `DROPLET_IP` from `.env`. In docs, use `1.2.3.4`.
- **No hardcoded domains** — use `example.com` variants in docs. Actual domains loaded at runtime via env vars.
- **No logging of secrets** — never log API keys, passwords, or tokens. Log actions and outcomes, not credential values.
- **`.env`, `*.tfvars`, `.env.test` are gitignored** — never commit them. Provide `.env.example` / `.env.test.example` with placeholder values.
- **Terraform state is gitignored** — `terraform.tfstate` contains sensitive data.

### 3.3 Type Safety

- **Python >= 3.11 required.** Use `X | None` union syntax natively. Docker images use `python:3.11-slim`.
- **Run `make typecheck` before deploying.** If mypy fails, do NOT push.
- **Every Python file must be covered by mypy.** Add new files to the mypy invocation in the Makefile immediately.
- **No `# type: ignore` without justification.** Fix the root cause. If suppression is unavoidable, include a reason: `# type: ignore[attr-defined]  # kraken lib has no stubs`.
- **Avoid `dict[str, Any]` round-trips.** Never use `model_dump()` → `dict` → `Model(**data)`. Use explicit keyword arguments or `model_copy(update=...)`.
- **Prefer strict `Literal` types over bare `str`** on Pydantic models when a field has a known set of valid values.
- **aiohttp middleware handler type** — do NOT use `web.RequestHandler` as the `handler` parameter type in `@web.middleware` functions. It is not callable under mypy strict. Use `Callable[[web.Request], Awaitable[web.StreamResponse]]` instead:

  ```python
  from collections.abc import Awaitable, Callable
  _Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

  @web.middleware
  async def auth_middleware(request: web.Request, handler: _Handler) -> web.StreamResponse:
      ...
  ```

### 3.4 Pydantic

- **Use `ConfigDict(extra="forbid")`** on models that define an external contract (webhook payloads, API responses).
- **Do not add defaults to fields that are always populated.** A default makes the field optional in JSON Schema and TypeScript types. Only use defaults for fields genuinely absent in some cases.
- **Use `Field(default_factory=list)`** for mutable defaults only when the field is genuinely optional.

### 3.5 Concurrency

- **Assume concurrency by default.** The listener is async (aiohttp). Any handler can be interrupted at an `await`.
- **Never use TOCTOU patterns with locks.** Lock acquisition must BE the check.
- **Financial operations require extra scrutiny** for race conditions, double-execution, partial failure, and idempotency.

### 3.6 Testing

- **Unit tests are colocated** next to the source file: `ws_parser.py` → `test_ws_parser.py`.
- **E2E tests live in `tests/e2e/`** within each service.
- **`make test`** runs all unit tests. **`make e2e-run`** runs E2E tests (requires Docker stack).
- **Always scope `unittest.mock.patch`.** Use `setUpModule()`/`tearDownModule()`, `self.addCleanup()`, `with patch():`, or `@patch()`. Never use bare `patcher.start()` without registering `.stop()`.
- **No cross-test dependencies.** Every test must be self-contained.
- **pytest** with `--import-mode=importlib`.

### 3.7 Docker

- **Never use `env_file:` in service definitions.** Always declare each env var explicitly in the `environment:` block with `${VAR}` interpolation.
- **`.dockerignore` uses an allowlist** (`*` to exclude everything, then `!services/listener/**` etc.). When adding a new standalone module (e.g. `services/notifier/`), add a `!services/<module>/**` entry.
- **Never nest bind mounts in `docker-compose.test.yml`.** If a service mounts `./services/poller:/app` and you also need `services/notifier/`, mount it at a separate path outside `/app` (e.g. `./services/notifier:/opt/notifier`) and add `PYTHONPATH: /opt` to the service's `environment:` block. Mounting inside the first mount causes Docker to auto-create empty directories on the host that shadow real content on restart.
- Runtime data MUST use Docker named volumes. Never write to the project directory.

### 3.8 Dependencies

- **Runtime deps** (`requirements.txt` per service): exact pins (`==`).
- **Dev deps** (`requirements-dev.txt`): major-version constraints (`>=X,<X+1`).

### 3.9 Model Naming Convention

All public-facing Pydantic models follow `{Action}{Resource}{InterfaceType}`:

| Suffix     | Meaning                          |
| ---------- | -------------------------------- |
| `Payload`  | Request body (POST/PUT JSON)     |
| `Response` | Response body returned to caller |
| `Params`   | Query parameters (GET)           |

Domain types (`BuySell`, `OrderStatus`) have no suffix.

---

## 4. File Structure

```
kraken_relay/
├── .env.example                 # Template — copy to .env, fill in values
├── .env.test.example            # Template for E2E test credentials
├── .gitignore
├── README.md                    # Project overview, setup, deploy, config
├── .github/
│   ├── copilot-instructions.md  # Agent guidelines (adapt from this blueprint)
│   └── workflows/
│       └── ci.yml               # GitHub Actions: lint → typecheck → test
├── docker-compose.yml           # Production stack (standalone mode)
├── docker-compose.shared.yml    # Override for shared-droplet mode
├── docker-compose.test.yml      # E2E test stack
├── docker-compose.local.yml     # Local dev override (direct port access)
├── .dockerignore                # Allowlist pattern
├── Makefile                     # All commands
├── pyproject.toml               # pytest, mypy, ruff config
├── requirements-dev.txt         # Dev dependencies (ruff, mypy, pytest, etc.)
├── schema_gen.py                # Pydantic → JSON Schema generator
├── cli/                         # Python CLI (operator scripts, stdlib only)
│   ├── __init__.py              # Project-specific config: CoreConfig setup, Kraken helpers
│   ├── __main__.py              # Entry point: registers core + project parsers, lazy dispatch
│   ├── poll.py                  # Trigger immediate poll via API (project-specific)
│   └── core/                    # Project-agnostic (copied from ibkr_relay as-is)
│       ├── __init__.py          # CoreConfig dataclass, generic helpers (env, SSH, DO API,
│       │                        #   Terraform, deploy_mode), register_parsers(), CORE_MODULES
│       ├── deploy.py            # Standalone (Terraform) or shared (rsync + compose)
│       ├── destroy.py           # Terraform destroy
│       ├── pause.py             # Snapshot + delete droplet
│       ├── resume.py            # Restore from snapshot
│       └── sync.py              # rsync files + pre-deploy checks + restart containers
├── services/
│   ├── listener/                # WebSocket listener service
│   │   ├── Dockerfile
│   │   ├── requirements.txt     # Runtime deps (exact pins)
│   │   ├── main.py              # Entrypoint (WS connection + HTTP health API)
│   │   ├── models_listener.py   # Pydantic models (webhook payloads, Kraken types)
│   │   ├── listener/            # Core WebSocket logic (package)
│   │   │   ├── __init__.py      # KrakenWS class, reconnection loop
│   │   │   ├── ws_parser.py     # Parse Kraken WS messages into Fill/Trade models
│   │   │   └── test_ws_parser.py
│   │   ├── routes/              # HTTP API
│   │   │   ├── __init__.py      # create_routes()
│   │   │   ├── middlewares.py   # Auth middleware (Bearer token)
│   │   │   └── health.py        # GET /health
│   │   └── tests/e2e/           # E2E tests
│   │       ├── conftest.py
│   │       ├── test_smoke.py
│   │       └── .env.test.example
│   ├── poller/                  # REST API poller service (backup)
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   ├── main.py              # Entrypoint (polling loop + HTTP API)
│   │   ├── models_poller.py     # Pydantic models (may share with listener)
│   │   ├── poller/              # Core polling logic (package)
│   │   │   ├── __init__.py      # poll_once(), watermark management
│   │   │   ├── rest_client.py   # Kraken REST API client (authenticated)
│   │   │   ├── test_rest_client.py
│   │   │   └── test_poller.py
│   │   ├── routes/
│   │   │   ├── __init__.py
│   │   │   ├── middlewares.py
│   │   │   └── run.py           # POST /kraken/poller/run (trigger immediate poll)
│   │   └── tests/e2e/
│   │       ├── conftest.py
│   │       └── test_smoke.py
│   └── notifier/                # Pluggable notification backends (library, no container)
│       ├── __init__.py          # Registry, load_notifiers(), validate_notifier_env(), notify()
│       ├── base.py              # BaseNotifier ABC (name, required_env_vars, send)
│       ├── webhook.py           # WebhookNotifier: HMAC-SHA256 signed HTTP POST
│       ├── test_notifier.py     # Tests for registry and loader
│       └── test_webhook.py      # Tests for webhook backend
│   └── dedup/                   # SQLite dedup (shared library, no container)
│       ├── __init__.py          # init_db(), is_processed(), get_processed_ids(),
│       │                        #   mark_processed(), mark_processed_batch(), prune()
│       └── test_dedup.py
├── infra/
│   └── caddy/
│       ├── Caddyfile            # Shell: imports from sites/ and domains/
│       ├── sites/
│       │   └── kraken.caddy     # SITE_DOMAIN route handlers (handle /kraken/*)
│       └── domains/             # Full site blocks (if project needs own domain)
├── types/                       # @tradegist/kraken-types npm package
│   ├── package.json
│   ├── index.d.ts
│   ├── listener/
│   │   ├── index.d.ts
│   │   ├── types.d.ts           # Generated from models_listener.py
│   │   └── types.schema.json
│   └── poller/
│       ├── index.d.ts
│       ├── types.d.ts           # Generated from models_poller.py
│       └── types.schema.json
└── terraform/
    ├── main.tf                  # Droplet + reserved IP + firewall
    ├── variables.tf             # All vars (with defaults + sensitive flags)
    ├── outputs.tf
    └── cloud-init.sh            # Docker install + creates project directory
```

### `.dockerignore` (allowlist pattern)

The `.dockerignore` uses an allowlist: deny everything (`*`), then selectively
allow source directories. Each service that `COPY`s code must be allowed here.

```
# Deny everything by default
*

# Allow service source code
!services/listener/**
!services/poller/**
!services/notifier/**
!services/dedup/**

# Re-exclude test files and caches from allowed dirs
services/listener/**/test_*.py
services/listener/**/__pycache__/
services/poller/**/test_*.py
services/poller/**/__pycache__/
services/notifier/**/test_*.py
services/notifier/**/__pycache__/
services/dedup/**/test_*.py
services/dedup/**/__pycache__/
```

When adding a new standalone module under `services/`, you MUST add a
`!services/<module>/**` entry here — otherwise `COPY` in the Dockerfile will
fail with a cryptic "not found" error.

### `.gitignore`

```gitignore
.env
.env.test
.pause-state
.venv/
terraform/.terraform/
terraform/.terraform.lock.hcl
terraform/terraform.tfstate
terraform/terraform.tfstate.backup
terraform/*.tfplan
*.tfvars
**/__pycache__/
```

**Do NOT add `.mypy_cache/`, `.pytest_cache/`, or `.ruff_cache/`** — these tools
auto-generate their own `.gitignore` file (containing `*`) inside their cache
directories on first run. Adding them to the project `.gitignore` is redundant.

**Do NOT add `.deployed-sha`** — this file only exists on the droplet (written
by `cli/sync.py` after each `--local-files` deploy). It is never present in the
local repo. It is excluded from rsync `--delete` so it isn't wiped on deploy,
but it does not need to be gitignored.

**Do NOT add `node_modules/`** — the `types/` package is declaration-only
(`.d.ts` files). There is no `npm install` step and no `node_modules/` to
ignore.

### Dockerfiles and cross-service modules

Each service Dockerfile must `COPY` shared packages (notifier, dedup) since
they are separate modules, not part of the service's own source:

```dockerfile
FROM python:3.11-slim
WORKDIR /app

COPY services/listener/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/listener/listener/ ./listener/
COPY services/listener/routes/ ./routes/
COPY services/listener/main.py services/listener/models_listener.py ./
COPY services/dedup/ ./dedup/
COPY services/notifier/ ./notifier/

CMD ["python", "main.py"]
```

Because shared packages are `COPY`'d from outside the service directory, the
Dockerfile's `context` must be the project root (`.`), not `./services/listener`:

```yaml
# docker-compose.yml
kraken-listener:
  build:
    context: .
    dockerfile: services/listener/Dockerfile
```

---

## 5. Architecture — Docker Containers

### Standalone Mode (3 containers)

| Service           | Role                                                                 |
| ----------------- | -------------------------------------------------------------------- |
| `kraken-listener` | WebSocket v2 connection, real-time fill detection, notifier delivery |
| `kraken-poller`   | REST API polling fallback, SQLite dedup, notifier delivery           |
| `caddy`           | Reverse proxy with automatic HTTPS                                   |

### Shared Mode (2 containers — no Caddy)

`kraken-listener` and `kraken-poller` only. Caddy is provided by the existing
stack on the droplet.

---

## 6. Service Details

### 6.1 Listener Service (`services/listener/`)

**Purpose:** Maintain a persistent WebSocket v2 connection to Kraken and fire
webhooks when order executions are detected.

**Kraken WebSocket v2 API:**

- Endpoint: `wss://ws-auth.kraken.com/v2`
- Authentication: Generate a WebSocket token via REST `POST /0/private/GetWebSocketsToken` using API key + secret.
- Subscribe to the `executions` channel to receive fill notifications.
- Messages arrive as JSON with trade details (txid, pair, price, volume, side, etc.).

**Core Loop (in `listener/__init__.py`):**

```
1. Get WS token from Kraken REST API (using API key + HMAC signature)
2. Connect to wss://ws-auth.kraken.com/v2
3. Subscribe to `executions` channel
4. For each message:
   a. Parse into Fill model (ws_parser.py) — raw fields (txid, pair) mapped to model fields (execId, symbol)
   b. Check dedup (services/dedup/) — skip if already processed
   c. Aggregate fills into Trade (by orderId / txid)
   d. Send via notifier (notify())
   e. Mark as processed in SQLite
5. On disconnect: exponential backoff reconnect (2s, 4s, 8s, ... max 60s)
```

**Reconnection:**

- Kraken WebSocket connections drop periodically (24h keepalive, network issues).
- The listener MUST reconnect automatically with exponential backoff.
- WS tokens expire after ~15 minutes — refresh token before reconnecting.
- Log reconnection attempts at `info` level. Do not log the token itself.

**HTTP API (aiohttp, same process):**

- `GET /health` — returns `{"status": "ok", "connected": true/false, "lastMessageAt": "..."}`.
- `GET /kraken/listener/status` — detailed status (uptime, message count, last trade).

**SQLite Dedup (shared `services/dedup/` module):**

Both the listener and poller use the shared `dedup` package for deduplication,
reading and writing the same `fills.db` at `DEDUP_DB_PATH` (default
`/data/dedup/fills.db`) on a shared `dedup-data` Docker named volume. SQLite
WAL mode + `timeout=5.0` enables safe concurrent access across containers.

- `processed_fills` table with `exec_id TEXT PRIMARY KEY` and `processed_at TEXT DEFAULT (datetime('now'))`, keyed by execution ID.
- Prune entries older than 30 days.

### 6.2 Poller Service (`services/poller/`)

**Purpose:** Periodically poll Kraken REST API for closed orders / trade history
as a reliability fallback. Catches fills that the WebSocket missed.

**Kraken REST API:**

- `POST /0/private/ClosedOrders` — list of closed orders.
- `POST /0/private/TradesHistory` — list of executed trades.
- Authentication: API-Key header + API-Sign (HMAC-SHA512 of nonce + POST data, keyed with base64-decoded API secret).

**Poll Cycle (in `poller/__init__.py`):**

```
1. Call exchange REST API for recent trades (TradesHistory)
2. Parse response JSON into Fill models
3. Batch dedup check via get_processed_ids() — skip already-processed IDs
4. Aggregate new fills into Trade models
5. Send via notifier (notify())
6. Batch mark processed via mark_processed_batch()
7. Update timestamp watermark in separate metadata DB
```

**Replay mode** (`replay > 0`):
- Passes `start=None` to the exchange API (ignores watermark, fetches all recent)
- Skips dedup entirely — takes the first N fills regardless
- Sends webhook but does NOT mark fills as processed or update watermark
- Use case: testing webhook delivery without waiting for new real trades

**HTTP API (aiohttp, same process):**

- `GET /health` — returns `{"status": "ok"}`.
- `POST /kraken/poller/run` — trigger immediate poll (auth required). Body: `{"replay": N}` to resend last N fills (bypasses dedup/watermark).

**SQLite:** Same shared dedup DB (`services/dedup/`) as listener — both services
read/write the same `fills.db` on the `dedup-data` volume. The poller has a
**separate** metadata database at `META_DB_PATH` (default `/data/meta/poller.db`)
on a private `poller-meta` volume for the timestamp watermark. The poller's
`init_dedup_db()` wraps `dedup.init_db()`; `init_meta_db()` creates and manages
the watermark table independently.

---

## 7. Pydantic Models

### `services/listener/models_listener.py`

> **Note:** The listener and poller may share the same webhook payload models.
> If they diverge, create `models_poller.py` separately. If identical, symlink
> or import from a shared location within the same service.

```python
from enum import Enum
from typing import Literal
from pydantic import BaseModel, ConfigDict

Source = Literal["ws_execution", "rest_poll"]

class BuySell(str, Enum):
    BUY = "buy"
    SELL = "sell"

class Fill(BaseModel):
    """Individual execution from exchange."""
    model_config = ConfigDict(extra="forbid")

    execId: str                  # Exchange execution/trade ID (unique per fill)
    orderId: str                 # Exchange order ID
    symbol: str                  # e.g. "XXBTZUSD"
    side: BuySell
    orderType: str               # "market", "limit", etc.
    price: float
    volume: float
    cost: float
    fee: float
    timestamp: str               # ISO 8601 ("2025-04-07T10:30:00Z")
    source: Source               # Origin: "ws_execution" or "rest_poll"
    # Add more fields as needed from the exchange's response

class Trade(BaseModel):
    """Aggregated trade (one or more fills for the same order)."""
    model_config = ConfigDict(extra="forbid")

    orderId: str
    symbol: str
    side: BuySell
    orderType: str
    price: float                 # Volume-weighted average price
    volume: float                # Total volume across fills
    cost: float                  # Total cost
    fee: float                   # Total fees
    fillCount: int
    execIds: list[str]           # All execution IDs in this trade
    timestamp: str               # ISO timestamp of latest fill
    source: Source               # Origin of the fills

class WebhookPayload(BaseModel):
    """Payload sent to the target webhook URL."""
    model_config = ConfigDict(extra="forbid")

    trades: list[Trade]
    errors: list[str]            # Parse errors, if any

SCHEMA_MODELS: list[type[BaseModel]] = [WebhookPayload, Trade, Fill]
```

### `services/poller/models_poller.py`

The poller re-exports shared types from the listener models (they are identical).
The listener's `models_listener.py` is the single source of truth.

```python
from models_listener import Fill, Trade, WebhookPayload

SCHEMA_MODELS: list[type[BaseModel]] = [WebhookPayload, Trade, Fill]
```

---

## 8. Webhook Delivery

Webhook delivery is handled by the **notifier** package (`services/notifier/`), a pluggable notification backend system shared across services.

- **`NOTIFIERS` env var** controls which backends are active (comma-separated, e.g. `NOTIFIERS=webhook`). Empty = no notifications (dry-run).
- **`WebhookNotifier`** is the built-in backend — it POSTs JSON payloads signed with HMAC-SHA256.

- **Body:** JSON-serialized `WebhookPayload`.
- **Signing:** HMAC-SHA256 of the body using `WEBHOOK_SECRET`.
- **Header:** `X-Signature-256: sha256=<hex_digest>`.
- **Optional extra header:** `WEBHOOK_HEADER_NAME` / `WEBHOOK_HEADER_VALUE` (for auth tokens on the receiving end).
- **Timeout:** 10 seconds.
- **Dry-run:** If `TARGET_WEBHOOK_URL` is empty, log the payload instead.

---

## 9. Auth Pattern

- API endpoints under `/kraken/*` require `Authorization: Bearer <API_TOKEN>` (HMAC-safe comparison via `hmac.compare_digest`).
- Webhook payloads are signed with HMAC-SHA256 (`X-Signature-256` header).
- Kraken API authentication uses API-Key + API-Sign (HMAC-SHA512 with nonce).

---

## 10. Environment Variables

### `.env.example`

```bash
# ── Deployment mode (REQUIRED) ──────────────────────────────────────
DEPLOY_MODE=standalone

# DigitalOcean API token (standalone mode only — can be removed after first deploy)
DO_API_TOKEN=your_digitalocean_api_token

# ── Kraken API ───────────────────────────────────────────────────────
KRAKEN_API_KEY=your-api-key-here
KRAKEN_API_SECRET=your-api-secret-here

# ── Webhook delivery ────────────────────────────────────────────────
NOTIFIERS=webhook
TARGET_WEBHOOK_URL=https://your-app.example.com/hooks/kraken
WEBHOOK_SECRET=generate-a-random-secret-here
WEBHOOK_HEADER_NAME=
WEBHOOK_HEADER_VALUE=

# ── Polling ─────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS=300

# ── API auth ────────────────────────────────────────────────────────
API_TOKEN=generate-a-random-token-here

# ── Infrastructure (standalone mode) ────────────────────────────────
SITE_DOMAIN=trade.example.com

# ── Droplet IP (from Terraform output, or provided by host) ────────
DROPLET_IP=your_droplet_ip_address

# ── SSH key (default: ~/.ssh/kraken-relay) ──────────────────────────
# For shared mode, set to the key provided by the droplet owner.
#SSH_KEY=~/.ssh/shared-droplet
```

---

## 11. docker-compose.yml

```yaml
name: kraken-relay

services:
  kraken-listener:
    build:
      context: .
      dockerfile: services/listener/Dockerfile
    restart: always
    environment:
      KRAKEN_API_KEY: ${KRAKEN_API_KEY:?Set KRAKEN_API_KEY in .env}
      KRAKEN_API_SECRET: ${KRAKEN_API_SECRET:?Set KRAKEN_API_SECRET in .env}
      NOTIFIERS: ${NOTIFIERS:-}
      TARGET_WEBHOOK_URL: ${TARGET_WEBHOOK_URL:-}
      WEBHOOK_SECRET: ${WEBHOOK_SECRET:-}
      WEBHOOK_HEADER_NAME: ${WEBHOOK_HEADER_NAME:-}
      WEBHOOK_HEADER_VALUE: ${WEBHOOK_HEADER_VALUE:-}
      API_TOKEN: ${API_TOKEN:?Set API_TOKEN in .env}
      DEDUP_DB_PATH: /data/dedup/fills.db
    expose:
      - "5000"
    volumes:
      - dedup-data:/data/dedup

  kraken-poller:
    build:
      context: .
      dockerfile: services/poller/Dockerfile
    restart: always
    environment:
      KRAKEN_API_KEY: ${KRAKEN_API_KEY:?Set KRAKEN_API_KEY in .env}
      KRAKEN_API_SECRET: ${KRAKEN_API_SECRET:?Set KRAKEN_API_SECRET in .env}
      NOTIFIERS: ${NOTIFIERS:-}
      TARGET_WEBHOOK_URL: ${TARGET_WEBHOOK_URL:-}
      WEBHOOK_SECRET: ${WEBHOOK_SECRET:-}
      WEBHOOK_HEADER_NAME: ${WEBHOOK_HEADER_NAME:-}
      WEBHOOK_HEADER_VALUE: ${WEBHOOK_HEADER_VALUE:-}
      POLL_INTERVAL_SECONDS: ${POLL_INTERVAL_SECONDS:-300}
      API_TOKEN: ${API_TOKEN:?Set API_TOKEN in .env}
      DEDUP_DB_PATH: /data/dedup/fills.db
      META_DB_PATH: /data/meta/poller.db
    expose:
      - "8000"
    volumes:
      - dedup-data:/data/dedup
      - poller-meta:/data/meta

  caddy:
    image: caddy:2-alpine
    restart: always
    ports:
      - "80:80"
      - "443:443"
    environment:
      SITE_DOMAIN: ${SITE_DOMAIN:?Set SITE_DOMAIN in .env}
    volumes:
      - ./infra/caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - ./infra/caddy/sites/:/etc/caddy/sites/:ro
      - ./infra/caddy/domains/:/etc/caddy/domains/:ro
      - /opt/caddy-shared/sites/:/etc/caddy/shared-sites/:ro
      - /opt/caddy-shared/domains/:/etc/caddy/shared-domains/:ro
      - caddy-data:/data
      - caddy-config:/config

volumes:
  dedup-data:
  poller-meta:
  caddy-data:
  caddy-config:

networks:
  default:
    name: relay-net
```

---

## 11.1. docker-compose.test.yml (E2E Test Overlay)

`docker-compose.test.yml` is a **Compose override file** applied on top of
`docker-compose.yml`. It is NOT a standalone file — it only works when composed:

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml -p kraken-relay-test --env-file .env.test ...
```

The Makefile wraps this as `E2E_COMPOSE`.

### Purpose

Transform the production stack into a local E2E test environment by:

1. **Binding host source code into containers** (volume mounts) — so code changes
   are picked up on `docker compose restart` without rebuilding images.
2. **Overriding env vars** with test-safe values (hardcoded tokens, dummy config).
3. **Exposing ports** on localhost so E2E tests (pytest + httpx) can call the APIs.
4. **Adding healthchecks** so `make e2e-up` can wait for readiness.
5. **Disabling production-only services** (Caddy) via `profiles: ["disabled"]`.

### Template

```yaml
# E2E test overrides — applied on top of docker-compose.yml.
# Usage: docker compose -f docker-compose.yml -f docker-compose.test.yml ...
# See Makefile E2E_COMPOSE for the full invocation.

services:
  kraken-listener:
    restart: "no"
    environment:
      API_TOKEN: test-token
      KRAKEN_API_KEY: ${KRAKEN_API_KEY} # from .env.test
      KRAKEN_API_SECRET: ${KRAKEN_API_SECRET} # from .env.test
      DEDUP_DB_PATH: /data/dedup/fills.db
      PYTHONPATH: /opt # for notifier + dedup at /opt/*
    ports:
      - "15010:5000"
    volumes:
      - ./services/listener:/app # bind-mount source for hot-reload
      - ./services/notifier:/opt/notifier # cross-service module (see below)
      - ./services/dedup:/opt/dedup # cross-service module
    healthcheck:
      test:
        [
          "CMD",
          "python",
          "-c",
          "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')",
        ]
      interval: 3s
      timeout: 5s
      retries: 20
      start_period: 10s

  kraken-poller:
    restart: "no"
    environment:
      API_TOKEN: test-token
      KRAKEN_API_KEY: ${KRAKEN_API_KEY}
      KRAKEN_API_SECRET: ${KRAKEN_API_SECRET}
      POLL_INTERVAL_SECONDS: "99999" # effectively disable auto-poll
      DEDUP_DB_PATH: /data/dedup/fills.db
      META_DB_PATH: /data/meta/poller.db
      PYTHONPATH: /opt
    ports:
      - "15011:8000"
    volumes:
      - ./services/poller:/app
      - ./services/notifier:/opt/notifier
      - ./services/dedup:/opt/dedup
    healthcheck:
      test:
        [
          "CMD",
          "python",
          "-c",
          "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')",
        ]
      interval: 3s
      timeout: 5s
      retries: 20
      start_period: 5s

  # Disable production-only services
  caddy:
    profiles: ["disabled"]
```

### Why bind mounts replace COPY'd code

In production, the Dockerfile `COPY`s source code into the image at build time.
In tests, a bind mount like `./services/listener:/app` **replaces the entire
`/app` directory** with the host's source folder. This means:

- Code edits are reflected instantly — `make e2e-run` does
  `docker compose restart` (no rebuild needed).
- But anything else that was `COPY`'d into `/app` by the Dockerfile is gone —
  including cross-service modules like `notifier/`.

### Cross-service modules (the PYTHONPATH trick)

Cross-service packages (`notifier`, `dedup`) are `COPY`'d into `/app/` by the
Dockerfile. But the bind mount `./services/listener:/app` wipes them. You need
separate mounts for each:

**WRONG** — nested mount inside `/app`:

```yaml
volumes:
  - ./services/listener:/app
  - ./services/notifier:/app/notifier # BROKEN: nested bind mount
```

Docker creates `services/listener/notifier/` on the host to back the nested
mount point. On `docker compose restart`, this empty host directory shadows the
real content → `ImportError`.

**CORRECT** — mount outside `/app`, add to `PYTHONPATH`:

```yaml
volumes:
  - ./services/listener:/app
  - ./services/notifier:/opt/notifier # separate path, no nesting
  - ./services/dedup:/opt/dedup
environment:
  PYTHONPATH: /opt # Python finds notifier + dedup at /opt/*
```

### Port convention

| Service           | Host port | Container port |
| ----------------- | --------- | -------------- |
| `kraken-listener` | 15010     | 5000           |
| `kraken-poller`   | 15011     | 8000           |

E2E tests connect to `http://localhost:15010` / `http://localhost:15011` with
`API_TOKEN: test-token`.

### `.env.test` and `.env.test.example`

Credentials for E2E tests live in `.env.test` (gitignored). Provide
`.env.test.example` as a template:

```bash
# Kraken API credentials for E2E tests (real account, small balance)
KRAKEN_API_KEY=your-test-api-key
KRAKEN_API_SECRET=your-test-api-secret
```

The Makefile passes `--env-file .env.test` so these override the production
values from `docker-compose.yml`.

### Restart behavior

`restart: "no"` on all services — test containers should not auto-restart on
failure. If something crashes, the test should fail, not retry silently.

---

## 11.2. docker-compose.local.yml (Local Dev Override)

`docker-compose.local.yml` is a Compose override for **local development** —
running the full stack on your machine with ports exposed directly (no Caddy,
no TLS).

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build
```

The Makefile wraps this as `make local-up` / `make local-down`.

### Template

```yaml
# Local dev overrides — direct port access, no Caddy.
services:
  kraken-listener:
    ports:
      - "5000:5000"
    volumes:
      - ./services/listener:/app
      - ./services/notifier:/opt/notifier
      - ./services/dedup:/opt/dedup
    environment:
      PYTHONPATH: /opt

  kraken-poller:
    ports:
      - "8000:8000"
    volumes:
      - ./services/poller:/app
      - ./services/notifier:/opt/notifier
      - ./services/dedup:/opt/dedup
    environment:
      PYTHONPATH: /opt

  caddy:
    profiles: ["disabled"]
```

### Differences from test overlay

| Aspect        | `docker-compose.local.yml`       | `docker-compose.test.yml`                      |
| ------------- | -------------------------------- | ---------------------------------------------- |
| Purpose       | Manual dev / debugging           | Automated E2E tests                            |
| Ports         | Standard (5000, 8000)            | Offset (15010, 15011) to avoid collisions      |
| Env overrides | Uses `.env` values               | Hardcoded `test-token`, `--env-file .env.test` |
| Healthchecks  | None                             | Added (for `make e2e-up` readiness wait)       |
| `restart`     | Inherits `always` from base      | `"no"` (fail-fast for tests)                   |
| Bind mounts   | Same pattern (source + notifier) | Same pattern                                   |

---

## 12. CLI Architecture — Core + Project Split

The CLI is split into two layers:

- **`cli/core/`** — project-agnostic commands and helpers, copied from `ibkr_relay` as-is.
- **`cli/__init__.py` + `cli/__main__.py` + project commands** — project-specific.

### `cli/core/__init__.py` — CoreConfig & Generic Helpers

All project-specific values are injected via a `CoreConfig` dataclass:

```python
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

@dataclass
class CoreConfig:
    project_name: str                              # e.g. "kraken-relay"
    project_dir: Path                              # Absolute path to repo root
    terraform_vars: dict[str, str]                  # TF_VAR_name → env-var-key
    required_env: list[str]                         # Env vars required for standalone deploy
    service_map: dict[str, str]                     # Alias → Docker Compose service name
    post_deploy_message: str = ""                   # Printed after standalone deploy
    post_resume_message: str = ""                   # Printed after resume
    compose_profiles_fn: Callable[[], str] | None = None  # Returns COMPOSE_PROFILES
    compose_env_fn: Callable[[], dict[str, str]] | None = None  # Returns extra env vars for compose
    size_selector_fn: Callable[[], str] | None = None     # Returns droplet size slug
    route_prefix: str = ""                           # Caddy site snippet route prefix (e.g. '/kraken')
    pre_sync_hook: Callable[[], None] | None = None       # Runs before sync

    @property
    def remote_dir(self) -> str:
        return f"/opt/{self.project_name}"

    def compose_profiles(self) -> str: ...
    def compose_env(self) -> str: ...    # Calls compose_env_fn, returns shell assignments
    def droplet_size(self) -> str: ...   # Falls back to "s-1vcpu-1gb"
```

Generic helpers exported: `die()`, `load_env()`, `env()`, `require_env()`,
`deploy_mode()`, `is_shared()`, `ssh_key_path()`, `ssh_cmd()`, `scp_file()`,
`do_api()`, `terraform()`, `set_config()`, `config()`.

Core also exports `CORE_MODULES` (command→module mapping) and
`register_parsers()` (registers deploy/destroy/pause/resume/sync subparsers).

### `cli/__init__.py` — Project-Specific Config

```python
from cli.core import CoreConfig, set_config

PROJECT_NAME = "kraken-relay"

_CONFIG = CoreConfig(
    project_name=PROJECT_NAME,
    project_dir=Path(__file__).resolve().parent.parent,
    terraform_vars={
        "do_token": "DO_API_TOKEN",
        "site_domain": "SITE_DOMAIN",
    },
    required_env=["DO_API_TOKEN", "KRAKEN_API_KEY", "KRAKEN_API_SECRET"],
    service_map={
        "listener": "kraken-listener",
        "kraken-listener": "kraken-listener",
        "poller": "kraken-poller",
        "kraken-poller": "kraken-poller",
        "caddy": "caddy",
    },
    route_prefix="/kraken",
)

set_config(_CONFIG)
```

### `cli/__main__.py` — Dispatch

```python
import cli  # triggers set_config()
from cli.core import CORE_MODULES, register_parsers

_PROJECT_MODULES: dict[str, str] = {
    "poll": "cli.poll",
}

def main():
    parser = argparse.ArgumentParser(description="Kraken Webhook Relay CLI")
    sub = parser.add_subparsers(dest="command")

    # Core commands (shared across projects)
    register_parsers(sub)

    # Project-specific commands
    p = sub.add_parser("poll", help="Trigger an immediate Kraken poll")
    p.add_argument("--replay", type=int, default=0, metavar="N",
                   help="Resend the last N fills (bypasses dedup/watermark)")

    modules = {**CORE_MODULES, **_PROJECT_MODULES}
    module = importlib.import_module(modules[args.command])
    module.run(args)
```

### Caddy Snippet Deployment (in shared deploy)

Handled by `_deploy_caddy_snippets()` in `cli/core/deploy.py`. During shared
deploy, all files under `infra/caddy/sites/` and `infra/caddy/domains/` are:

1. **Templated** — all `{$VAR}` patterns replaced with literal env var values.
   If a referenced env var is not set, the deploy fails with an error.
2. **Copied** to `/opt/caddy-shared/{sites,domains}/` on the droplet.
3. **Caddy is reloaded** via `docker exec caddy caddy reload`.

This allows shared projects to use `{$KRAKEN_WS_DOMAIN}` in their snippets
locally, while the deployed version contains the literal domain — no env var
injection needed in the host’s Caddy container.

**Namespace validation** — every `handle` directive in `sites/*.caddy` must
start with `route_prefix` (from `CoreConfig`). Validated after templating,
before SCP:

```python
def _validate_site_snippet_routes(content: str, snippet_name: str, prefix: str) -> None:
    for match in re.finditer(r'^\s*handle\s+(\S+)', content, re.MULTILINE):
        path = match.group(1)
        if not path.startswith(f"{prefix}/"):
            die(f"Snippet {snippet_name}: handle path '{path}' does not start "
                f"with project prefix '{prefix}/'. All site snippet routes "
                f"must be namespaced under '{prefix}/*' to avoid collisions.")
```

---

## 13. Makefile Targets

```makefile
PROJECT = kraken-relay
PYTHON ?= .venv/bin/python3

setup:        ## Create .venv and install all dependencies
deploy:       ## Deploy (Terraform if standalone, rsync if shared)
destroy:      ## Terraform destroy
sync:         ## Push .env + restart (LOCAL_FILES=1 for full sync)
test:         ## Run unit tests
typecheck:    ## Run mypy strict
lint:         ## Run ruff (FIX=1 to auto-fix)
types:        ## Regenerate TypeScript types from Pydantic models
e2e-up:       ## Start E2E test stack
e2e-run:      ## Run E2E tests
e2e-down:     ## Stop E2E test stack
e2e:          ## Full E2E cycle (up + run + down)
local-up:     ## Start local dev stack
local-down:   ## Stop local dev stack
poll:         ## Trigger immediate Kraken poll (REPLAY=N to resend last N fills)
```

### `poll` target with replay support

```makefile
poll:
	$(PYTHON) -m cli poll $(if $(V),-v) $(if $(REPLAY),--replay $(REPLAY))
```

### `MYPYPATH` for cross-service imports

Each `make typecheck` invocation sets `MYPYPATH` so mypy can resolve imports
across service boundaries. **When one service imports models from another** (e.g.
the poller imports `models_listener` from the listener), both service dirs must
be on `MYPYPATH`:

```makefile
typecheck:
	MYPYPATH=services/listener:services $(PYTHON) -m mypy services/listener/
	MYPYPATH=services/poller:services/listener:services $(PYTHON) -m mypy services/poller/
	MYPYPATH=services $(PYTHON) -m mypy services/notifier/
	$(PYTHON) -m mypy services/dedup/
```

The poller needs `services/listener` on its path because `models_poller.py`
re-exports from `models_listener`. Without it, mypy reports
`"Skipping analyzing 'models_listener': module is installed, but missing
library stubs or py.typed marker"`.

### `PYTHONPATH` for tests

The `test` target sets `PYTHONPATH` to include all service directories so
pytest can resolve cross-service imports:

```makefile
test:
	PYTHONPATH=.:services/listener:services/poller:services $(PYTHON) -m pytest -v
```

---

## 14. pyproject.toml

```toml
[project]
requires-python = ">=3.11"

[tool.mypy]
strict = true
warn_return_any = true
warn_unused_configs = true
explicit_package_bases = true

[tool.pytest.ini_options]
testpaths = ["services/listener", "services/poller", "services/notifier", "services/dedup"]
norecursedirs = ["tests/e2e"]
addopts = "--import-mode=importlib"

[tool.ruff]
target-version = "py311"
line-length = 100
src = ["services/listener", "services/poller", "services/notifier", "services/dedup", "cli"]

[tool.ruff.lint]
select = ["F", "E", "W", "I", "UP", "B", "SIM", "RUF", "PGH003"]
ignore = ["E501"]

[tool.ruff.lint.isort]
known-first-party = ["listener", "poller", "notifier", "dedup", "routes", "models_listener", "models_poller"]
```

---

## 15. Terraform

Terraform provisions the DigitalOcean infrastructure (standalone mode only).

**Resources created:**

1. **SSH Key** — auto-generated ED25519 keypair (`tls_private_key`). Public key registered with DO as `kraken-relay-deploy`. Private key output as Terraform sensitive output, **auto-saved** to `~/.ssh/kraken-relay` (chmod 600) by `cli/deploy.py` after `terraform apply`.
2. **Droplet** — Ubuntu 24.04 LTS, `s-1vcpu-1gb` ($6/mo) or `s-1vcpu-2gb` ($12/mo). User data runs `cloud-init.sh` which only installs Docker and creates `/opt/kraken-relay/`.
3. **Reserved IP** — static public IP assigned to droplet (survives reboots).
4. **Firewall** — inbound SSH (port 22) restricted to deployer IP (auto-detected via `api.ipify.org`), inbound HTTP/HTTPS (80/443) open to all, all outbound allowed.

**Naming:** `kraken-relay` (droplet), `kraken-relay-fw` (firewall), `kraken-relay-deploy` (SSH key).

**`cloud-init.sh`** installs Docker and creates the project directory. The CLI handles the rest: rsync files → push `.env` → `docker compose up -d --build`.

---

## 16. Deployment Model

- **`make sync LOCAL_FILES=1`** uses rsync to `/opt/kraken-relay/` on the droplet.
- **Guards:** Must be on `main` branch with clean working tree.
- **Pre-deploy checks:** `make lint`, `make typecheck`, `make test` (and optionally E2E).
- **rsync `--delete`** removes stale files. Runtime data is in Docker volumes (safe).
- **`.deployed-sha`** records the deployed commit SHA.
- **Project directory contains only source files.** All runtime data uses Docker named volumes.

---

## 17. CI Workflow (`.github/workflows/ci.yml`)

```yaml
name: CI
on: [push, pull_request]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r requirements-dev.txt -r services/listener/requirements.txt -r services/poller/requirements.txt
      - name: Lint
        run: make lint
      - name: Typecheck
        run: make typecheck
      - name: Test
        run: make test
```

Order: lint → typecheck → test (fastest to slowest, fail early).

---

## 18. Implementation Order

Build in this sequence. Run `make lint`, `make typecheck`, and `make test` after
every step.

1. **Scaffold** — repo init, `.gitignore`, `README.md`, `pyproject.toml`, `requirements-dev.txt`, `Makefile` (setup/lint/typecheck/test targets), empty `services/` dirs.
2. **Models** — `models_listener.py` with `Fill`, `Trade`, `WebhookPayload`, `BuySell`. Write tests.
3. **Dedup** — `services/dedup/` shared module (SQLite init, check, mark, prune). Write tests.
4. **Notifier** — `services/notifier/` (base ABC, webhook backend, registry, loader). Copy from `ibkr_relay` and adapt. Write tests.
5. **WS Parser** — `listener/ws_parser.py` (parse Kraken WS JSON into Fill models). Write tests with sample messages.
6. **Listener core** — `listener/__init__.py` (WS connect, subscribe, reconnect loop, integration of parser + dedup + notifier).
7. **HTTP health API** — `routes/health.py`, `routes/middlewares.py`.
8. **Listener entrypoint** — `main.py` (start WS + HTTP concurrently).
9. **Dockerfile + docker-compose.yml** — containerize listener.
10. **Poller** — same sequence (REST client → parser → routes → main → Dockerfile). Dedup is already shared from step 3.
11. **CLI** — Copy `cli/core/` from `ibkr_relay` as-is. Write `cli/__init__.py` with Kraken `CoreConfig`. Write `cli/__main__.py` importing `register_parsers()` + `CORE_MODULES`. Add `cli/poll.py` (project-specific).
12. **Terraform** — `main.tf`, `variables.tf`, `outputs.tf`, `cloud-init.sh`, `env.tftpl`.
13. **Caddy** — `Caddyfile` (standalone), `kraken.caddy` (shared snippet).
14. **Shared mode** — `docker-compose.shared.yml`, snippet validation, deploy integration.
15. **TypeScript types** — `schema_gen.py`, `types/` package, `make types`.
16. **E2E tests** — against a real Kraken account with API keys (paper/small balance).
17. **CI** — GitHub Actions workflow.

---

## 19. Kraken API Reference

Use the official docs for exact field names, auth signatures, and WebSocket
message formats:

- **REST API:** https://docs.kraken.com/api/
- **WebSocket v2:** https://docs.kraken.com/api/docs/websocket-v2/
- **Authentication:** https://docs.kraken.com/api/docs/guides/authentication
- **Executions channel:** https://docs.kraken.com/api/docs/websocket-v2/executions

The agent should fetch these docs to get the exact JSON schemas for WS messages
and REST responses before writing parsers.

---

## 20. Resource Footprint

| Aspect           | Value                                     |
| ---------------- | ----------------------------------------- |
| Data sources     | Kraken WS v2 (real-time) + REST (polling) |
| Auth complexity  | API key + HMAC signature (simple)         |
| Containers       | 2-3 (pure Python, no JVM)                 |
| Memory footprint | ~80MB total                               |
| Parser input     | JSON                                      |
| Real-time data   | Yes (WebSocket)                           |
| Project prefix   | `/kraken/`                                |
