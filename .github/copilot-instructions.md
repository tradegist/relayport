# IBKR Webhook Relay — Project Guidelines

## Code Quality (MANDATORY)

- **Always apply best practices by default.** Do not ask the user whether to follow a best practice — just do it. Use idiomatic Python naming, file organization, and patterns. When there is a clearly better approach (naming, structure, error handling), use it directly and explain why.

## Security Rules (MANDATORY)

- **No hardcoded credentials** — passwords, API tokens, secrets, and keys MUST come from environment variables (`.env` file or `TF_VAR_*`). Never write real values in source files.
- **No hardcoded IPs** — use `DROPLET_IP` from `.env`. In documentation, use `1.2.3.4` as placeholder.
- **No hardcoded domains** — use `example.com` variants (`vnc.example.com`, `trade.example.com`) in docs and code. Actual domains are loaded at runtime via `VNC_DOMAIN` / `TRADE_DOMAIN` env vars.
- **No email addresses or personal info** — never write real names, emails, or account IDs in committed files. Use `UXXXXXXX` for IBKR account examples.
- **No logging of secrets** — never `log.info()` or `print()` tokens, passwords, or API keys. Log actions and outcomes, not credential values.
- **`.env`, `*.tfvars`, and `.env.test` are gitignored** — never commit them. Use `.env.example` / `.env.test.example` with placeholder values as reference.
- **Terraform state is gitignored** — `terraform.tfstate` contains SSH keys and IPs. Never commit it.

## Type Safety (MANDATORY)

- **Python >= 3.11 is required.** The project uses `X | None` union syntax natively (no `from __future__ import annotations`). Docker images use `python:3.11-slim`. Local dev uses a `.venv` created from the latest Homebrew Python.
- **Run `make typecheck` before copying ANY Python file to the droplet.** This is non-negotiable. If mypy fails, do NOT push the code.
- **Run `make test` before assuming work is done and before copying ANY file to the droplet.** If tests fail, fix them first. Never deploy untested code.
- **Run `make test` and `make typecheck` after every code change**, even refactors. Do not wait until the end — verify immediately.
- **Run E2E tests after adding or modifying any E2E test.** E2E tests require the Docker stack — `make test` (unit tests) does not run them. Never assume an E2E test passes without actually running the stack. The E2E workflow is:
  1. `make e2e-up` — start the stack (idempotent, skips if already running).
  2. `make e2e-run` — run the tests.
  3. Fix code → `make e2e-run` → repeat until all tests pass. Volume mounts keep code in sync — no rebuild needed.
  4. `make e2e-down` — tear down **only after all tests pass**. Never tear down between iterations.
- When modifying any Python file (`.py`), always run `make test` and `make typecheck` and confirm both pass before deploying.
- **Every Python file must be covered by `make typecheck`.** When adding a new Python service, package, or standalone script, immediately add it to the mypy invocation in the Makefile. No Python file may exist outside mypy's scope.
- After modifying any model in `poller/models.py` or `remote-client/models.py`, also run `make types` to regenerate the TypeScript definitions.
- **Always verify type safety by breaking it first.** After any refactor that touches types or model construction, deliberately introduce a type error (e.g. pass a `str` where `float` is expected), run `make typecheck`, and confirm it **fails**. Then revert and confirm it passes. Never assume mypy catches something — prove it.
- **Avoid `dict[str, Any]` round-trips.** Never use `model_dump()` → `dict` → `Model(**data)` — mypy cannot type-check `**dict[str, Any]`. Use explicit keyword arguments or `model_copy(update=...)` instead.

## Pydantic Best Practices

- **Use `Field(default_factory=list)`** for mutable defaults (`list`, `dict`). Never use bare `[]` or `{}` as default values — it risks shared mutable state.
- **Use `ConfigDict(extra="forbid")`** on models that define an external contract (e.g. webhook payloads, API responses). This produces `additionalProperties: false` in the JSON Schema, keeping generated TypeScript types strict (no `[k: string]: unknown`).
- **Docstrings on `parse_fills()` and similar claim "never raises"** — ensure the implementation matches. Wrap any call that can throw (e.g. `ET.fromstring()`) in try/except and return errors in the result tuple.

## Concurrency Safety (MANDATORY)

- **Assume concurrency by default.** Both services are async (aiohttp). Any handler can be interrupted at an `await`. When writing new code, always consider what happens if two requests arrive at the same time.
- **Always be wary of race conditions.** Before merging any code that touches shared state, ask: "Can two callers interleave here? What breaks if they do?"
- **Never use TOCTOU (Time of Check, Time of Use) patterns with locks.** Do NOT check `lock.locked()` and then `async with lock:` — another coroutine can acquire the lock between the check and the acquisition, defeating the guard. This is a race condition.
- **Lock acquisition must BE the check.** Use `asyncio.wait_for(lock.acquire(), timeout=0)` with `try/finally: lock.release()` to fail-fast, or accept that `async with lock:` will queue. Never separate "is it locked?" from "acquire it."
- **This applies to all shared-state guards** — locks, database transactions, file locks, semaphores, balance checks. If the action is "check a condition, then act on it," both steps must be atomic.
- **Financial operations require extra scrutiny.** Any code path that places orders, moves money, or modifies account state must be reviewed for: race conditions, double-execution, partial failure (what if it crashes between two steps?), and idempotency.

## Local Development

- **`.venv` is the project's virtual environment.** Created by `make setup` using Homebrew Python. All dev dependencies are installed there.
- **Auto-activation** is configured in `~/.zshrc` via a `chpwd` hook — the venv activates automatically when `cd`'ing into the project directory.
- **`make setup`** creates the `.venv` (if missing) and installs all dependencies (`requirements-dev.txt` + both service requirements).
- **`.venv/` is gitignored** — never commit it.

## Dependency Management

- **Runtime deps (`poller/requirements.txt`, `remote-client/requirements.txt`)** use exact pins (`==`). These are deployed to production containers — builds must be reproducible.
- **Dev deps (`requirements-dev.txt`)** use major-version constraints (`>=X,<X+1`). This allows minor/patch updates while preventing breaking changes.
- **When adding a new dependency**, always pin it immediately — never leave it unpinned. Use exact pin for runtime, major-version constraint for dev.

## Docker

- **`.dockerignore` uses an allowlist** (`*` to exclude everything, then `!poller/**` to include the whole module). Tests, `__pycache__`, and the Dockerfile itself are re-excluded. This means adding new source files to `poller/` requires **no** `.dockerignore` or Dockerfile changes.
- The poller Dockerfile uses directory COPYs (`COPY poller/poller/ ./poller/`, `COPY poller/routes/ ./routes/`) so new files are picked up automatically.

## Architecture

Six Docker containers in a single Compose stack on a DigitalOcean droplet:

| Service              | Role                                                                           |
| -------------------- | ------------------------------------------------------------------------------ |
| `ib-gateway`         | IBKR Gateway (gnzsnz/ib-gateway). Restart policy: `on-failure` (not `always`). |
| `novnc`              | Browser VNC proxy for 2FA authentication                                       |
| `caddy`              | Reverse proxy with automatic HTTPS (Let's Encrypt)                             |
| `webhook-relay`      | Python API server — places orders via IB Gateway                               |
| `poller`             | Polls IBKR Flex for trade confirmations, fires webhooks                        |
| `gateway-controller` | Lightweight sidecar — starts ib-gateway container via Docker socket            |

All secrets are injected via `.env` → `env_file` or `environment` in `docker-compose.yml`.
Caddy reads `VNC_DOMAIN` and `TRADE_DOMAIN` from env vars — the Caddyfile uses `{$VNC_DOMAIN}` / `{$TRADE_DOMAIN}` syntax.

## Memory & Droplet Sizing

- `JAVA_HEAP_SIZE` in `.env` controls IB Gateway's JVM heap (in MB, default 768, max 10240).
- **Droplet size is auto-selected** by Terraform based on this value (see `locals` block in `main.tf`).
- `cli/resume.py` mirrors the same size-selection logic in Python.

## Auth Pattern

- API endpoints under `/ibkr/*` require `Authorization: Bearer <API_TOKEN>` (HMAC-safe comparison via `hmac.compare_digest`).
- Webhook payloads are signed with HMAC-SHA256 (`X-Signature-256` header).
- VNC access is password-protected (VNC protocol auth).

## IB Gateway Lifecycle

- `TWOFA_TIMEOUT_ACTION: exit` — gateway exits cleanly on 2FA timeout (no restart loop).
- `RELOGIN_AFTER_TWOFA_TIMEOUT: "no"` — prevents automatic re-login attempts.
- `restart: on-failure` — Docker restarts only on crashes, not clean exits.
- Sessions last ~1 week before IBKR forces re-authentication.

## E2E Testing

- **E2E tests run against a local Docker stack** with a real IB Gateway connected to a paper trading account. Real orders are placed in paper mode.
- **Credentials live in `.env.test`** (gitignored). Template: `.env.test.example`.
- **`docker-compose.test.yml`** at project root defines the test stack (ib-gateway + webhook-relay only, no Caddy/poller/VNC).
- **`make e2e`** starts the stack, waits for connection, runs pytest, then tears down. Always cleans up, even on test failure.
- **`make e2e-up` / `make e2e-down`** for manual stack management during debugging.
- **Test API runs on `localhost:15000`** with hardcoded token `test-token`.
- **No healthcheck on `ib-gateway`** — the `IBClient.connect()` handles retry with exponential backoff, same as production.
- **Paper accounts require no 2FA**, so the E2E stack is fully automated.

## Remote Client Structure

The `remote-client/` service is organized into packages:

```
remote-client/
  main.py                  # Entrypoint (connection + HTTP server)
  models.py                # Pydantic request/response models (order API)
  client/                  # IB Gateway client (namespace delegation)
    __init__.py            # IBClient class (connection management)
    orders.py              # OrdersNamespace: place(contract_req, order_req)
  routes/                  # HTTP route handlers
    __init__.py            # Orchestrator: create_routes()
    middlewares.py         # Auth middleware (Bearer token)
    order_place.py         # POST /ibkr/order
    health.py              # GET /health
  tests/e2e/               # E2E tests (paper account)
    conftest.py            # httpx fixtures (api + anon_api)
    .env.test.example      # Template for paper credentials
```

- **One file per route** — easy to find and scale.
- **Namespace delegation for IBClient** — `client.orders.place(contract_req, order_req)`. Add new namespaces (e.g. `holdings.py`, `quotes.py`) as needed.
- **Route handlers access the client via `request.app["client"]`**, not closures.

## Poller Structure

The `poller/` service follows the same package pattern:

```
poller/
  main.py                  # Entrypoint (polling loop + HTTP API startup)
  models.py                # Pydantic models: Fill, Trade, WebhookPayload, BuySell
  poller/                  # Core polling logic (package)
    __init__.py            # SQLite dedup, webhook delivery, Flex fetch, poll_once()
    flex_parser.py         # XML parser (Activity Flex + Trade Confirmation)
    test_flex_parser.py    # Tests for flex_parser
    test_poller.py         # Tests for poller core logic
  routes/                  # HTTP API
    __init__.py            # Orchestrator: create_routes(), start_api_server()
    middlewares.py         # Auth middleware (Bearer token)
    run.py                 # POST /ibkr/poller/run handler
  Dockerfile
  requirements.txt
```

- **`poller/poller/`** contains core logic: SQLite dedup, webhook delivery (HMAC-SHA256), Flex Web Service two-step fetch, and `poll_once()`.
- **`poller/routes/`** contains the HTTP API for on-demand polls (`POST /ibkr/poller/run`).
- **`poller/models.py`** is the source of truth for TypeScript types (`make types`).

## Models (Two Separate Files)

This project has **two independent `models.py` files** — they serve different concerns and share no code:

| File                      | Domain                      | Contains                                                                                 |
| ------------------------- | --------------------------- | ---------------------------------------------------------------------------------------- |
| `poller/models.py`        | Webhook payloads (outbound) | `Fill`, `Trade`, `WebhookPayload`, `BuySell` — parsed from IBKR Flex XML                 |
| `remote-client/models.py` | Order API (inbound)         | `ContractRequest`, `OrderRequest`, `PlaceOrderRequest`, `OrderResponse` — REST API types |

- `poller/models.py` is the source of truth for `IbkrPoller` TypeScript types (`make types`).
- `remote-client/models.py` is the source of truth for `IbkrHttp` TypeScript types (`make types`).
- `remote-client/models.py` uses strict `Literal` types (`Action`, `OrderType`, `SecType`, `TimeInForce`) aligned with `ib_async` field names.
- Both use `ConfigDict(extra="forbid")` for strict validation.

## Order API Payload

The `POST /ibkr/order` endpoint accepts a nested payload mirroring `ib.placeOrder(contract, order)`:

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
    "orderType": "LMT",
    "lmtPrice": 150.0
  }
}
```

- Field names match `ib_async` exactly (e.g. `lmtPrice`, `totalQuantity`, `secType`, `tif`, `outsideRth`).
- `contract.secType` defaults to `"STK"`, `contract.exchange` to `"SMART"`, `contract.currency` to `"USD"`.
- `order.tif` defaults to `"DAY"`, `order.outsideRth` to `false`.
- Pydantic validates the full request; invalid payloads return 400 with structured error details.

## TypeScript Types

- Types are published as `@tradegist/ibkr-types` (npm package in `types/`, not yet published).
- **Two namespaces**: `IbkrPoller` (webhook payload types) and `IbkrHttp` (order API types).
- **`make types`** regenerates both from Pydantic models:
  - `poller/models.py` → `types/poller/webhook.d.ts`
  - `remote-client/models.py` → `types/http/order.d.ts`
- **Structure:**
  ```
  types/
    index.d.ts                 # Barrel: exports IbkrPoller, IbkrHttp namespaces
    package.json               # @tradegist/ibkr-types
    poller/
      index.d.ts               # Re-exports: BuySell, WebhookPayload, Trade
      webhook.d.ts             # Generated from poller/models.py
      webhook.schema.json      # Intermediate JSON Schema
    http/
      index.d.ts               # Re-exports: PlaceOrderRequest, ContractRequest, OrderRequest, OrderResponse
      order.d.ts               # Generated from remote-client/models.py
      order.schema.json        # Intermediate JSON Schema
  ```
- **Usage:** `import { IbkrPoller, IbkrHttp } from "@tradegist/ibkr-types"`
- Both `models.py` files have `__main__` blocks that output JSON Schema to stdout (used by the Makefile).

## Code Style

- Python: `logging` module, f-strings, `aiohttp` for async HTTP in both webhook-relay and poller, `httpx` for sync HTTP client in poller.
- CLI scripts: Python (`cli/` package), invoked via `python3 -m cli <command>` or `make`. Uses only stdlib (`subprocess`, `urllib.request`, `json`, `os`). No third-party dependencies.
- Terraform: all secrets marked `sensitive = true` in `variables.tf`.

## Build & Deploy

All commands available via `make` or `python3 -m cli <command>`:

```bash
make deploy    # Terraform init + apply (reads .env)
make sync      # Push .env to droplet + restart services
make destroy   # Terraform destroy
make pause     # Snapshot + delete droplet (save costs)
make resume    # Restore from snapshot
make poll      # Trigger immediate Flex poll
make order     # Place an order
make e2e       # Run E2E tests (paper account)
```

Direct CLI (no Make required, works on Windows):

```bash
python3 -m cli deploy
python3 -m cli sync gateway
python3 -m cli order 2 TSLA MKT
python3 -m cli poll 2
```

## File Structure

```
.env.example            # Template — copy to .env and fill in real values
docker-compose.yml      # All 6 services
cli/                    # Python CLI (operator scripts)
  __init__.py           # Shared helpers (env loading, SSH, DO API, validation)
  __main__.py           # Entry point (python3 -m cli <command>)
  deploy.py             # Terraform init + apply
  destroy.py            # Terraform destroy
  pause.py              # Snapshot + delete droplet
  resume.py             # Restore from snapshot
  sync.py               # Push .env + restart services
  order.py              # Place orders via HTTPS API
  poll.py               # Trigger immediate Flex poll
caddy/Caddyfile         # Reverse proxy config (uses env vars for domains)
remote-client/          # webhook-relay service (see Remote Client Structure above)
poller/                 # Flex poller service (see Poller Structure above)
  models.py             # Pydantic models: Fill, Trade, WebhookPayload, BuySell
gateway-controller/     # CGI sidecar (Alpine, busybox httpd)
novnc/index.html        # Custom VNC UI (Tailwind CSS)
types/                  # @tradegist/ibkr-types npm package (IbkrPoller + IbkrHttp namespaces)
docker-compose.test.yml # E2E test stack
terraform/              # Infrastructure as code (DigitalOcean)
```
