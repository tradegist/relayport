# IBKR Webhook Relay — Project Guidelines

## Security Rules (MANDATORY)

- **No hardcoded credentials** — passwords, API tokens, secrets, and keys MUST come from environment variables (`.env` file or `TF_VAR_*`). Never write real values in source files.
- **No hardcoded IPs** — use `DROPLET_IP` from `.env`. In documentation, use `1.2.3.4` as placeholder.
- **No hardcoded domains** — use `example.com` variants (`vnc.example.com`, `trade.example.com`) in docs and code. Actual domains are loaded at runtime via `VNC_DOMAIN` / `TRADE_DOMAIN` env vars.
- **No email addresses or personal info** — never write real names, emails, or account IDs in committed files. Use `UXXXXXXX` for IBKR account examples.
- **No logging of secrets** — never `log.info()` or `print()` tokens, passwords, or API keys. Log actions and outcomes, not credential values.
- **`.env` and `*.tfvars` are gitignored** — never commit them. Use `.env.example` with placeholder values as reference.
- **Terraform state is gitignored** — `terraform.tfstate` contains SSH keys and IPs. Never commit it.

## Type Safety (MANDATORY)

- **Run `make typecheck` before copying ANY Python file to the droplet.** This is non-negotiable. If mypy fails, do NOT push the code.
- **Run `make test` before assuming work is done and before copying ANY file to the droplet.** If tests fail, fix them first. Never deploy untested code.
- When modifying any Python file (`.py`), always run `make test` and `make typecheck` and confirm both pass before deploying.
- After modifying any model in `models.py`, also run `make types` to regenerate the TypeScript definitions.
- **Always verify type safety by breaking it first.** After any refactor that touches types or model construction, deliberately introduce a type error (e.g. pass a `str` where `float` is expected), run `make typecheck`, and confirm it **fails**. Then revert and confirm it passes. Never assume mypy catches something — prove it.
- **Avoid `dict[str, Any]` round-trips.** Never use `model_dump()` → `dict` → `Model(**data)` — mypy cannot type-check `**dict[str, Any]`. Use explicit keyword arguments or `model_copy(update=...)` instead.

## Pydantic Best Practices

- **Use `Field(default_factory=list)`** for mutable defaults (`list`, `dict`). Never use bare `[]` or `{}` as default values — it risks shared mutable state.
- **Use `ConfigDict(extra="forbid")`** on models that define an external contract (e.g. webhook payloads, API responses). This produces `additionalProperties: false` in the JSON Schema, keeping generated TypeScript types strict (no `[k: string]: unknown`).
- **Docstrings on `parse_fills()` and similar claim "never raises"** — ensure the implementation matches. Wrap any call that can throw (e.g. `ET.fromstring()`) in try/except and return errors in the result tuple.

## Dependency Management

- **Runtime deps (`poller/requirements.txt`, `remote-client/requirements.txt`)** use exact pins (`==`). These are deployed to production containers — builds must be reproducible.
- **Dev deps (`requirements-dev.txt`)** use major-version constraints (`>=X,<X+1`). This allows minor/patch updates while preventing breaking changes.
- **When adding a new dependency**, always pin it immediately — never leave it unpinned. Use exact pin for runtime, major-version constraint for dev.

## Docker

- **`.dockerignore` uses an allowlist** (`*` to exclude everything, then `!` to include only what the Dockerfile needs). This prevents `.env`, `.git/`, `terraform/`, and other sensitive files from leaking into the Docker build context.
- When modifying a Dockerfile to COPY new files, update `.dockerignore` to allow them.

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

## Code Style

- Python: `logging` module, f-strings, `aiohttp` for async HTTP in webhook-relay, `httpx` for sync HTTP in poller.
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
remote-client/          # webhook-relay service (Python, aiohttp)
poller/                 # Flex poller service (Python, httpx)
gateway-controller/     # CGI sidecar (Alpine, busybox httpd)
novnc/index.html        # Custom VNC UI (Tailwind CSS)
terraform/              # Infrastructure as code (DigitalOcean)
```
