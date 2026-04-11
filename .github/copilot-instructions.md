# IBKR Webhook Relay — Project Guidelines

## Code Quality (MANDATORY)

- **Always apply best practices by default.** Do not ask the user whether to follow a best practice — just do it. Use idiomatic Python naming, file organization, and patterns. When there is a clearly better approach (naming, structure, error handling), use it directly and explain why.
- **No unused imports.** After writing or editing any Python file, verify every `import` is actually used in the file. Remove any that are not. This applies to new files and edits to existing files alike.
- **No `__all__`.** All imports are explicit (`from module import X`). `__all__` only controls star-imports, which we never use.
- **No `assert` for runtime guards.** `assert` is stripped under `python -O`, turning invariant checks into silent `None`/`AttributeError`. Use `if ... raise RuntimeError(...)` (or `die()`) for any check that must hold at runtime.
- **Makefile must mirror CLI arguments.** When adding a new parameter to a `cli/` command, always add the corresponding `$(if $(VAR),--flag $(VAR))` to the Makefile target so `make <target> VAR=value` works.
- **Update README.md when changing public interfaces.** When adding or modifying CLI commands, Makefile targets, API endpoints, or env vars, always update the README to reflect the change.
- **Run `make lint` after every code change.** Ruff enforces unused imports (F401), import ordering (I001), unused variables, common pitfalls (bugbear), and modern Python idioms. If ruff fails, fix before committing. Use `make lint FIX=1` to auto-fix safe issues (import sorting, etc.).
- **Register new modules in `pyproject.toml`.** When adding a new Python service, package, or standalone module under `services/`, immediately add it to all four places in `pyproject.toml`: (1) `tool.pytest.ini_options.testpaths`, (2) `tool.ruff.src`, (3) `tool.ruff.lint.isort.known-first-party`, and (4) the mypy invocation in the Makefile. Missing any of these causes silent miscategorisation (isort), missed tests (pytest), or unchecked code (mypy).
- **Centralise env var reads into typed getter functions.** Each env var must be read in exactly one place — a getter function in the module that owns it (e.g. `get_flex_token()` in `poller/__init__.py`). The getter applies `.strip()` and any type conversion (`int()`, boolean parsing). All other code — including other modules, `main.py` entrypoints, and route handlers — imports and calls the getter. Never call `os.environ.get()` inline except inside a getter. This eliminates duplicated reads, inconsistent `.strip()`, and scattered default values.
- **Getters must validate and fail fast.** Every getter that reads an env var must validate the value and raise `SystemExit` with a descriptive message on invalid input — never let a bad value propagate silently. For required string vars (no default), check emptiness: `if not val: raise SystemExit("IBKR_FLEX_TOKEN must be set")`. For `int()` conversions, wrap in `try/except ValueError: raise SystemExit(f"Invalid VAR={raw!r} — must be an integer")`. For boolean flags, parse inside the getter (`flag not in ("0", "false", "no")`) and return `bool`. Callers should never need to validate a getter's return value — if the getter returns, the value is valid.
- **Prefer pure functions over side-effect functions.** Never write an `apply_*()` / `set_*()` function that silently mutates system state (env vars, globals, module-level caches) as its primary purpose. Instead, compute and return the value — let the caller decide how to use it. For example, instead of `apply_debug_url_override()` that mutates `os.environ`, write a `resolve_url()` that returns the URL and let the consumer store it. If a side-effect function is truly unavoidable (e.g. one-time DB migration), add an inline comment at every call site explaining **what** is mutated and **why**: `# Mutates os.environ["X"] to enable Y`.
- **Never bulk-set `os.environ` with empty-string fallbacks.** A loop like `os.environ[key] = env(name, "")` silently overrides downstream defaults (e.g. Terraform `variable` defaults, library config) with empty strings — the downstream system sees the variable as *set but empty* instead of *unset*, which breaks `tonumber()`, validation blocks, and non-string parsing. When bridging env vars to another system (Terraform `TF_VAR_*`, subprocess env, etc.), only export a key when the source value is present and non-empty. Explicitly `os.environ.pop(key, None)` otherwise so stale values from a previous run don't leak through.

## Security Rules (MANDATORY)

- **No hardcoded credentials** — passwords, API tokens, secrets, and keys MUST come from environment variables (`.env` file or `TF_VAR_*`). Never write real values in source files.
- **No hardcoded IPs** — use `DROPLET_IP` from `.env`. In documentation, use `1.2.3.4` as placeholder.
- **No hardcoded domains** — use `example.com` variants (`trade.example.com`) in docs and code. Actual domains are loaded at runtime via `SITE_DOMAIN` env var.
- **No email addresses or personal info** — never write real names, emails, or account IDs in committed files. Use `UXXXXXXX` for IBKR account examples.
- **No logging of secrets or sensitive operational data** — never `log.info()` or `print()` tokens, passwords, or API keys. Log actions and outcomes, not credential values. When adding any `log.info()` or `log.debug()` call, check whether the logged value contains sensitive fields (e.g. `accountId`, `acctAlias`, account numbers, IPs, domains). Never log full model dumps at `info` level — use `log.debug` with explicit field exclusion: `log.debug("Trade: %s", trade.model_dump_json(exclude={"accountId", "acctAlias"}))`. Prefer logging counts, symbols, and statuses over full objects.
- **`.env`, `*.tfvars`, and `.env.test` are gitignored** — never commit them. Use `.env.example` / `.env.test.example` with placeholder values as reference.
- **Terraform state is gitignored** — `terraform.tfstate` contains SSH keys and IPs. Never commit it.
- **Auth middleware must reject empty `API_TOKEN`.** `hmac.compare_digest("", "")` returns `True`, so an empty `API_TOKEN` env var silently disables authentication. Every auth middleware must check `if not _API_TOKEN:` and return HTTP 500 **before** reaching `compare_digest`. `API_TOKEN` is in `required_env` for deploy/sync — the CLI will block deployment if it is missing or empty.

## Type Safety (MANDATORY)

- **Python >= 3.11 is required.** The project uses `X | None` union syntax natively (no `from __future__ import annotations`). Docker images use `python:3.11-slim`. Local dev uses a `.venv` created from the latest Homebrew Python.
- **Run `make typecheck` before copying ANY Python file to the droplet.** This is non-negotiable. If mypy fails, do NOT push the code.
- **Run `make test` before assuming work is done and before copying ANY file to the droplet.** If tests fail, fix them first. Never deploy untested code.
- **Run `make test` and `make typecheck` after every code change**, even refactors. Do not wait until the end — verify immediately.
- **Run E2E tests after modifying any E2E test OR infrastructure file.** Infrastructure files include `docker-compose*.yml`, `Dockerfile`, `Caddyfile`, and anything under `infra/`. E2E tests require the Docker stack — `make test` (unit tests) does not run them. Never assume an E2E test passes without actually running the stack. The E2E workflow is:
  1. `make e2e-up` — start the stack (idempotent, skips if already running).
  2. `make e2e-run` — run the tests.
  3. Fix code → `make e2e-run` → repeat until all tests pass. Volume mounts keep code in sync — no rebuild needed.
  4. `make e2e-down` — tear down **only after all tests pass**. Never tear down between iterations.
- When modifying any Python file (`.py`), always run `make test`, `make typecheck`, and `make lint` and confirm all pass before deploying.
- **Every Python file must be covered by `make typecheck`.** When adding a new Python service, package, or standalone script, immediately add it to the mypy invocation in the Makefile. No Python file may exist outside mypy's scope.
- After modifying any model in `services/poller/poller_models.py` or `services/shared/__init__.py`, also run `make types` to regenerate the TypeScript definitions.
- **Always verify type safety by breaking it first.** After any refactor that touches types or model construction, deliberately introduce a type error (e.g. pass a `str` where `float` is expected), run `make typecheck`, and confirm it **fails**. Then revert and confirm it passes. Never assume mypy catches something — prove it.
- **Avoid `dict[str, Any]` round-trips.** Never use `model_dump()` → `dict` → `Model(**data)` — mypy cannot type-check `**dict[str, Any]`. Use explicit keyword arguments or `model_copy(update=...)` instead.
- **Prefer strict `Literal` types over bare `str` on Pydantic models.** Financial applications demand precision — a `str` field silently accepts typos and invalid values. When a field has a known set of valid values (e.g. `BuySell`, `OrderType`, `AssetClass`), always use the existing `Literal` type. Only fall back to `str` when the external source (e.g. Flex XML) genuinely returns unbounded values — and document why with an inline comment.
- **No `# type: ignore` without justification.** Do not bypass the type checker. Fix the root cause instead — use proper type annotations, import the correct type, widen a dict annotation, or use `cast()`. If suppression is truly unavoidable (e.g. untyped third-party library), the comment must include a reason: `# type: ignore[attr-defined] # ib_async.Foo has no stubs`. A bare `# type: ignore` with no explanation is never acceptable.
- **Use `@overload` for sentinel-default patterns.** When a function accepts an optional default via a sentinel (e.g. `_UNSET = object()`), use `@overload` to express the two call signatures (`def f(key: str) -> str` and `def f(key: str, default: str) -> str`) instead of `# type: ignore` on the return. Use `cast()` in the implementation body for the default branch.

## Pydantic Best Practices

- **Use `Field(default_factory=list)`** for mutable defaults (`list`, `dict`) **only when the field is genuinely optional.** Never use bare `[]` or `{}` as default values — it risks shared mutable state.
- **Do not add defaults to fields that are always populated.** A default (`= 0`, `= ""`, `= Field(default_factory=list)`) makes the field optional in the generated JSON Schema and TypeScript types (e.g. `fillCount?: number`). If the construction code always provides the value, the field must be required (no default) so the schema reflects the true contract. Only use defaults for fields that are legitimately absent in some cases (e.g. XML attributes that may be missing).
- **Use `ConfigDict(extra="forbid")`** on models that define an external contract (e.g. webhook payloads, API responses). This produces `additionalProperties: false` in the JSON Schema, keeping generated TypeScript types strict (no `[k: string]: unknown`).
- **Docstrings on `parse_fills()` and similar claim "never raises"** — ensure the implementation matches. Wrap any call that can throw (e.g. `ET.fromstring()`) in try/except and return errors in the result tuple.

## Error Handling (MANDATORY)

- **Every error must produce a clear, actionable message.** Whether the consumer is an API caller or a developer reading logs, the error must explain _what_ failed and _why_. Never raise or return a generic "something went wrong" — include the relevant context (operation, input identifier, upstream status code, etc.).
- **API responses must never leak internal details.** Return structured error JSON with an appropriate HTTP status code and a human-readable `error` field. Never expose raw Python tracebacks, file paths, or internal class names to API callers. Log the full exception server-side at `error`/`exception` level for debugging.
- **Isolate failures — one bad component must not take down the system.** When dispatching to multiple backends, plugins, or external services, wrap each call in `try/except Exception`, log the failure, and continue. A single broken notifier, webhook endpoint, or third-party API must not crash the poll cycle, block other notifiers, or kill the HTTP server.
- **Never silently swallow errors.** Every `except` block must either log the exception (`log.exception(...)`) or re-raise. A bare `except: pass` is never acceptable — it hides bugs and makes debugging impossible.
- **Use `log.exception()` for unexpected errors.** It automatically includes the traceback at `ERROR` level. Reserve `log.error()` for known/expected failure conditions where a traceback would be noise.
- **Distinguish recoverable from fatal errors.** Recoverable errors (network timeout, temporary API failure) should be logged and retried or skipped. Fatal errors (missing required config, corrupted state) should fail fast with `raise SystemExit(msg)` or `die()` and a clear message — do not attempt to limp along.
- **`SystemExit` must carry a descriptive message.** Never `raise SystemExit(1)` — callers that catch `SystemExit` (e.g. `validate_notifier_env()`) lose all context about what failed. Always `raise SystemExit("Notifier 'webhook' requires env vars: WEBHOOK_SECRET")` so the message can be surfaced to the user. Log the error at the raise site as well.
- **Env var parsing must fail fast, not fall back silently.** When parsing an env var with `int()`, `float()`, or similar, wrap in `try/except ValueError` and `raise SystemExit(f"Invalid VAR={raw!r} — must be an integer")`. Never silently fall back to a default on parse failure — that hides config mistakes. Falling back is only appropriate for _missing_ env vars (where the default is the intended behavior), not for _invalid_ values.
- **Validate at system boundaries, trust internally.** Validate all external inputs (API payloads, env vars, webhook data, Flex XML responses) at the point of entry. Once validated, internal code should not re-validate — the type system and Pydantic models carry the guarantees.
- **Never assume a default for financial enum fields.** When mapping external data to a constrained set (e.g. buy/sell side, order type), validate that the value is an exact match. Never use an `else` branch that silently assigns a default — e.g. `BuySell.BUY if x == "buy" else BuySell.SELL` treats _any_ non-buy value (including typos, nulls, and garbage) as SELL. Always check every valid value explicitly and raise/error on unknown input. This applies to all trade direction, order type, asset class, and similar mappings.
- **`fee` is always positive (amount paid).** This is the industry standard (FIX protocol, Alpaca, Coinbase, Kraken). IBKR Flex XML reports commissions as negative numbers (`ibCommission="-0.62"`); the parser normalizes with `abs()` so `Fill.fee` and `Trade.fee` always represent the positive amount paid. Never store or forward negative fee values — consumers should not need to guess the sign convention.
- **Never silently drop rows with missing identifiers.** When parsing external data (Flex XML, REST JSON, WebSocket messages), if a required identifier (e.g. `execId`) is missing or empty after all fallback chains, report it as a parse error and skip the row explicitly. Do not let it fall through to a later guard (like a dedup check on empty string) where the drop is invisible. Every skipped row must produce an error message explaining _why_ it was skipped.
- **HTTP handlers must catch and map exceptions.** Every route handler must have a top-level `try/except` that catches unexpected errors and returns a proper HTTP error response (500 with structured JSON). Unhandled exceptions in aiohttp handlers produce ugly default responses and can leak internals.
- **Include context in error messages.** Bad: `"Failed to fetch Flex report"`. Good: `"Failed to fetch Flex report: query 12345 — HTTP 500 from IBKR"`. The message should contain enough detail to diagnose without consulting logs.

## Reliability (MANDATORY)

- **Mark-after-notify, never before.** `mark_processed_batch()` must only run AFTER `notify()` completes successfully. A crash between mark and notify silently drops fills — the fill is recorded as processed but the webhook was never sent. The poller (dedup skips it) will never retry it. This is an unrecoverable data loss.
- **The correct pattern:** run `notify()` and `mark_processed_batch()` sequentially in the same execution context (same thread or same `asyncio.to_thread` call). If `notify()` raises, the fill remains unprocessed and will be retried on the next cycle.
- **Never separate mark from notify with an `await` boundary.** In async code, an `await` between mark and notify allows the process to crash between the two operations. Keep them atomic within a single synchronous block (e.g. inside `asyncio.to_thread`).
- **Replay mode is the exception.** `poll --replay N` intentionally skips dedup — it resends the last N fills without marking them. This is by design for debugging/recovery.
- **SQLite commits must be explicit.** After any `INSERT`/`UPDATE` to SQLite (dedup DB or metadata DB), call `conn.commit()` immediately. Without an explicit commit, a crash loses the write silently. Never rely on implicit commit behavior.

## Concurrency Safety (MANDATORY)

- **Assume concurrency by default.** The poller is async (aiohttp). Any handler can be interrupted at an `await`. When writing new code, always consider what happens if two requests arrive at the same time.
- **Always be wary of race conditions.** Before merging any code that touches shared state, ask: "Can two callers interleave here? What breaks if they do?"
- **Never use TOCTOU (Time of Check, Time of Use) patterns with locks.** Do NOT check `lock.locked()` and then `async with lock:` — another coroutine can acquire the lock between the check and the acquisition, defeating the guard. This is a race condition.
- **Lock acquisition must BE the check.** Use `asyncio.wait_for(lock.acquire(), timeout=0)` with `try/finally: lock.release()` to fail-fast, or accept that `async with lock:` will queue. Never separate "is it locked?" from "acquire it."
- **This applies to all shared-state guards** — locks, database transactions, file locks, semaphores, balance checks. If the action is "check a condition, then act on it," both steps must be atomic.
- **Never share a `sqlite3.Connection` across threads.** `sqlite3.Connection` is not thread-safe. When using `asyncio.to_thread()`, either pass the connection into a single synchronous function that does all DB work in one thread, or use an `asyncio.Lock` to ensure only one `to_thread()` call uses the connection at a time. Never allow two concurrent `to_thread()` calls to touch the same connection — this causes intermittent `OperationalError` and data corruption.
- **Poller `to_thread` pattern: create connections inside the worker thread.** Do NOT create `sqlite3.Connection` on the main (event-loop) thread and pass it into `asyncio.to_thread(poll_once, conn, ...)` — even with `check_same_thread=False`, this is cross-thread use and unsafe. Instead, `poll_once()` accepts `dedup_conn=None, meta_conn=None` and creates thread-local connections internally (via `init_dedup_db()` / `init_meta_db()`), closing them in a `finally` block. The caller (`_poll_loop`, `handle_run_poll`) passes only non-DB arguments. This ensures every `to_thread` call uses connections that were both created and closed on the same worker thread.
- **Financial operations require extra scrutiny.** Any code path that places orders, moves money, or modifies account state must be reviewed for: race conditions, double-execution, partial failure (what if it crashes between two steps?), and idempotency.
- **Use `asyncio.get_running_loop()`, never `asyncio.get_event_loop()`.** `get_event_loop()` is deprecated since Python 3.10 for contexts without a running loop and emits `DeprecationWarning` in 3.12+. Code that calls `loop.call_later()`, `loop.create_task()`, etc. always runs on the event-loop thread, so `get_running_loop()` is correct, explicit, and raises `RuntimeError` immediately if accidentally called off-loop.

## Local Development

- **`.venv` is the project's virtual environment.** Created by `make setup` using Homebrew Python. All dev dependencies are installed there.
- **Auto-activation** is configured in `~/.zshrc` via a `chpwd` hook — the venv activates automatically when `cd`'ing into the project directory.
- **`make setup`** creates the `.venv` (if missing), installs all dependencies (`requirements-dev.txt` + service requirements), and writes a `.pth` file (see below).
- **`ibkr-relay.pth`** is created inside `.venv/lib/pythonX.Y/site-packages/` by `make setup`. It adds `services/poller/`, `services/debug/`, and `services/` to `sys.path` so that `from poller_models import ...`, `from debug_app import ...`, and `from notifier import ...` work everywhere (CLI, tests, scripts) without `sys.path` hacks or `PYTHONPATH`.
- **`.venv/` is gitignored** — never commit it.
- **`docker-compose.local.yml` adds bind mounts** that shadow the `COPY`'d files in the image with your local source tree (`:ro`). This means code changes are visible on container restart — no rebuild needed. `make local-up` builds the images once; after that, `make sync` (when `DEFAULT_CLI_RELAY_ENV=local`) just restarts containers.
- **`make sync` respects `DEFAULT_CLI_RELAY_ENV`.** When set to `local`, `make sync` restarts the local compose stack. When `prod` (default), it runs the full CLI sync to the droplet. Override per-command with `ENV=local` or `ENV=prod`.
- **`make logs` also respects `DEFAULT_CLI_RELAY_ENV`.** `make logs S=ibkr-debug` streams local container logs when local, droplet logs when prod.

## Dependency Management

- **Runtime deps (`services/poller/requirements.txt`)** use exact pins (`==`). These are deployed to production containers — builds must be reproducible.
- **Dev deps (`requirements-dev.txt`)** use major-version constraints (`>=X,<X+1`). This allows minor/patch updates while preventing breaking changes.
- **When adding a new dependency**, always pin it immediately — never leave it unpinned. Use exact pin for runtime, major-version constraint for dev.
- **All services pinning the same dependency must use the same version.** When multiple `requirements.txt` files pin the same package (e.g. `aiohttp`), keep versions aligned. Check existing pins with `grep -r 'aiohttp==' services/*/requirements.txt` before adding a new one.

## Docker

- **Never use `env_file:` in service definitions.** Always declare each env var explicitly in the `environment:` block with `${VAR}` interpolation. This is critical because `env_file:` is internally a list — override files append rather than replace, causing the production `.env` to leak into test containers. Explicit `environment:` vars with `--env-file` interpolation keeps environments fully isolated and allows clean overrides.
- **`POLLER_ENABLED=false`** disables the poller container entirely. Implemented via `deploy.replicas: ${POLLER_REPLICAS:-1}` in `docker-compose.yml`. The mapping from `POLLER_ENABLED` to `POLLER_REPLICAS` happens in `cli/__init__.py` (`_compose_env()`) and the Makefile (`POLLER` flag). The derived `POLLER_REPLICAS` is injected as a shell env var in the SSH command (not in `.env`), so it takes precedence over the compose file default.
- **`DEBUG_WEBHOOK_PATH`** enables the `ibkr-debug` container. When set (non-empty) in `.env`, `_compose_env()` sets `DEBUG_REPLICAS=1`; otherwise the compose default `${DEBUG_REPLICAS:-0}` keeps the container stopped. The debug service has aggressive log rotation (`max-size: 10k`, `max-file: 1`) since its sole purpose is transient payload inspection. Set `DEBUG_LOG_LEVEL=DEBUG` in `.env` to include full payload+headers in `docker logs`.
- **`.dockerignore` uses an allowlist** (`*` to exclude everything, then `!services/poller/**` to include the whole module). Tests, `__pycache__`, and the Dockerfile itself are re-excluded. This means adding new source files to `services/poller/` requires **no** `.dockerignore` or Dockerfile changes.
- **When adding a new standalone module** (e.g. `services/notifier/`), you must add a `!services/<module>/**` entry to `.dockerignore` — the allowlist excludes everything by default. Also add exclusions for test files and `__pycache__` under the new module. Without this, `COPY services/<module>/ ./<module>/` in the Dockerfile will fail with a cryptic "not found" error.
- The poller Dockerfile uses directory COPYs (`COPY services/poller/poller/ ./poller/`, `COPY services/poller/poller_routes/ ./poller_routes/`) so new files are picked up automatically.
- **`poller-2` must mirror `poller` configuration.** The `poller-2` service is an optional second poller instance (behind the `poller2` profile) for a different IBKR account. Its `environment:` and `volumes:` blocks must stay in sync with `poller` — same env var names (with `_2` suffix for account-specific values), and its own `META_DB_PATH` (e.g. `/data/meta/poller-2.db`) on a dedicated `poller-2-data` volume. When modifying the `poller` service block, always check whether `poller-2` needs the same change.
- **Never nest bind mounts in compose override files.** If a service mounts `./services/poller:/app` and you also need `services/notifier/` available, do NOT mount `./services/notifier:/app/notifier` (inside the first mount). Docker will auto-create an empty `services/poller/notifier/` directory on the host to back the nested mount point. On `docker compose restart`, this empty host directory shadows the real content, causing `ImportError`. Instead, mount the extra module at a separate path outside `/app` (e.g. `./services/notifier:/opt/notifier`) and add `PYTHONPATH: /opt` to the service's `environment:` block so Python can find it.

## Architecture

Four Docker containers in a single Compose stack on a DigitalOcean droplet:

| Service      | Role                                                                                                                                               |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `caddy`      | Reverse proxy with automatic HTTPS (Let's Encrypt)                                                                                                 |
| `poller`     | Polls IBKR Flex for trade confirmations, fires webhooks. Disabled via `POLLER_ENABLED=false`                                                       |
| `poller-2`   | Optional second poller instance for a different IBKR account. Behind `poller2` profile                                                             |
| `ibkr-debug` | Debug webhook inbox — captures webhook payloads for inspection. Disabled by default (`DEBUG_REPLICAS=0`), enabled when `DEBUG_WEBHOOK_PATH` is set |

All secrets are injected via `.env` → `environment` in `docker-compose.yml`.
Caddy reads `SITE_DOMAIN` from env vars — the Caddyfile uses `{$SITE_DOMAIN}` syntax.

### Caddy Snippet Structure

The Caddyfile uses `import` directives to compose routing from snippet files:

```
infra/caddy/
  Caddyfile              # Shell: imports from sites/ and shared dirs
  sites/
    ibkr.caddy           # SITE_DOMAIN route handlers (handle /ibkr/*)
    debug.caddy          # Debug webhook routes (handle /debug/webhook/*)
```

Shared projects deploy snippets to `/opt/caddy-shared/sites/` on the droplet (not into the host project's directory). The host Caddy mounts both:

- `./infra/caddy/sites/` → `/etc/caddy/sites/` (host project's own routes)
- `/opt/caddy-shared/sites/` → `/etc/caddy/shared-sites/` (shared projects' routes)

During shared deploy, snippet files are **templated** — all `{$VAR}` placeholders are replaced with literal env var values from the shared project's `.env`. This avoids requiring the host Caddy container to have the shared project's env vars.

- **`sites/*.caddy`** contain `handle` blocks imported inside the `{$SITE_DOMAIN}` site definition. Each project writes one snippet (e.g. `ibkr.caddy`, `kraken.caddy`). Routes must be prefixed with the project name (`/ibkr/*`, `/kraken/*`) to avoid collisions. The `debug.caddy` snippet routes `/debug/webhook/*` to the `ibkr-debug` container.
- This structure allows multiple projects to share a single Caddy instance on the same droplet.

## Deployment Modes

The deployment mode is controlled by `DEPLOY_MODE` in `.env` (required, validated before any deploy or sync).

### Standalone Mode (`DEPLOY_MODE=standalone`)

- Set `DO_API_TOKEN` in `.env`. `make deploy` runs Terraform to create a new droplet, firewall, and reserved IP, then the CLI rsyncs project files, pushes `.env`, and runs `docker compose up -d --build`.
- Terraform only creates infrastructure — cloud-init installs Docker and creates the project directory. The CLI handles all file transfer and service startup.
- After deploy, add `DROPLET_IP` from terraform output to `.env` for `make sync`.
- `DO_API_TOKEN` can be removed after first deploy for security — the mode is determined by `DEPLOY_MODE`, not by token presence.

### Shared Mode (`DEPLOY_MODE=shared`)

- Set `DROPLET_IP` and `SSH_KEY` in `.env` (no `DO_API_TOKEN` needed).
- `make deploy` rsyncs files, pushes `.env`, and starts services using `docker-compose.shared.yml` overlay.
- The shared overlay disables Caddy (the host project runs it) and connects all containers to the shared Docker network (`SHARED_NETWORK` env var, typically `relay-net`).
- **`SHARED_NETWORK` controls cross-project networking.** The base `docker-compose.yml` uses `name: ${SHARED_NETWORK:-}` for the default network. When unset, Docker Compose creates a project-scoped network (isolated). When set to the same value across projects (e.g. `relay-net`), all projects share a single network and can reach each other's containers by service name. The shared overlay (`docker-compose.shared.yml`) sets the network to `external: true`, which merges on top of the base definition.
- Caddy snippet files (`infra/caddy/sites/ibkr.caddy`) must be deployed to the host project's Caddy to enable routing.
- `make sync` uses the shared compose overlay automatically.

## Droplet Sizing

- **`DROPLET_SIZE`** sets the DigitalOcean droplet slug directly (e.g. `s-1vcpu-512mb`). This is a poller-only deployment — no JVM heap sizing needed.
- `cli/__init__.py` `_droplet_size()` reads `DROPLET_SIZE`.
- `cli/core/resume.py` uses `cfg.droplet_size()` which delegates to the same `_droplet_size()` function.

## Auth Pattern

- API endpoints under `/ibkr/*` require `Authorization: Bearer <API_TOKEN>` (HMAC-safe comparison via `hmac.compare_digest`).
- **All authenticated routes must use the `AUTH_PREFIX` constant** (from `poller_routes.middlewares`) when registering with the router. The auth middleware uses the same constant to decide which requests require a token — hardcoding the path in either place causes them to drift out of sync.
- Webhook payloads are signed with HMAC-SHA256 (`X-Signature-256` header) via the notifier package.

## E2E Testing

- **E2E tests run against a local Docker stack** defined by `docker-compose.test.yml` (poller + ibkr-debug, no Caddy).
- **Credentials live in `.env.test`** (gitignored). Template: `.env.test.example`.
- **`make e2e`** starts the stack, runs pytest, then tears down. Always cleans up, even on test failure.
- **`make e2e-up` / `make e2e-down`** for manual stack management during debugging.
- **`make e2e-run`** restarts `poller` and `ibkr-debug` containers (to pick up code changes from volume mounts), then runs the E2E tests. Safe to call repeatedly during development — no need to rebuild or restart manually.
- **Test poller runs on `localhost:15011`** with hardcoded token `test-token`.

## Test File Convention

- **Unit tests are colocated** next to the source file they test: `flex_parser.py` → `test_flex_parser.py`, `poller/__init__.py` → `test_poller.py`.
- **E2E tests live in `tests/e2e/`** within each service, since they test multiple components together rather than a single source file.
- **`make test`** runs all unit tests. **`make e2e-run`** runs all E2E tests (requires Docker stack). **`make lint`** runs ruff. All must pass before deploying.
- **Always scope `unittest.mock.patch`.** Never call `patch.start()` at module level without a corresponding `patch.stop()` — the patched value leaks into every test module that runs afterward. Use one of these patterns instead:
  - **`setUpModule()` / `tearDownModule()`** — for module-wide patches (e.g. `API_TOKEN` that all tests in the file need).
  - **`self.addCleanup(patcher.stop)`** in `setUp()` — for class-scoped patches.
  - **`with patch(...):`** inside the test — for single-test patches.
  - **`@patch(...)`** decorator — for single-test or single-class patches.
  - Never use bare `_patcher.start()` without registering a `.stop()`.
- **Use `setUpModule()` / `tearDownModule()` for env var overrides.** When tests need specific `os.environ` values, save originals in `setUpModule()` and restore in `tearDownModule()`. Never mutate `os.environ` at module level without cleanup — the mutation leaks into every test module that runs afterward. The pattern:

  ```python
  _ORIG_ENV: dict[str, str | None] = {}
  _TEST_ENV = {"MY_VAR": "test-value"}

  def setUpModule() -> None:
      for key, val in _TEST_ENV.items():
          _ORIG_ENV[key] = os.environ.get(key)
          os.environ[key] = val

  def tearDownModule() -> None:
      for key, orig in _ORIG_ENV.items():
          if orig is None:
              os.environ.pop(key, None)
          else:
              os.environ[key] = orig
  ```

  Both functions are called automatically by pytest/unittest — no manual invocation needed. Prefer this over `mock.patch.dict(os.environ, ...)` when the env vars must be set for the entire module (e.g. all test classes). For single-test env changes, use `with mock.patch.dict(os.environ, ...):` instead.

- **Avoid reading env vars at module level in production code.** Module-level `os.environ` reads (e.g. `DEBUG_PATH = os.environ.get(...)`) bake values at import time, forcing tests to set env vars before imports — a fragile anti-pattern. Defer env reads to a factory function (e.g. `create_app()`) or constructor so tests can set env vars normally in `setUpModule()` and get fresh reads on each call.
- **No cross-test dependencies.** Every test must be self-contained — it must not rely on state created by a previous test. Pytest does not guarantee execution order, and tests may run selectively or in parallel. If a test needs preconditions, create them within the test itself or via an explicit fixture.
- **E2E conftest fixtures must use `yield` with a context manager.** Never `return httpx.Client(...)` — the client is never closed and leaks sockets. Use `with httpx.Client(...) as client: yield client` instead. Scope to `session` (one client per test run). Every E2E `conftest.py` must also include a `_preflight_check` fixture (`scope="session"`, `autouse=True`) that hits `/health` and calls `pytest.exit()` if the stack is unreachable.

## Poller Structure

The `services/poller/` service follows the same package pattern:

```
services/poller/
  main.py                  # Entrypoint (polling loop + HTTP API startup)
  poller_models.py         # Re-export shim (shared models + poller-specific API types)
  poller/                  # Core polling logic (package)
    __init__.py            # SQLite dedup, Flex fetch, poll_once()
    flex_parser.py         # XML parser (Activity Flex + Trade Confirmation)
    test_flex_parser.py    # Tests for flex_parser
    test_poller.py         # Tests for poller core logic
  poller_routes/            # HTTP API
    __init__.py            # Orchestrator: create_routes(), start_api_server()
    health.py              # GET /health handler
    middlewares.py         # Auth middleware (Bearer token)
    run.py                 # POST /ibkr/poller/run handler
    test_middlewares.py    # Tests for auth middleware
  tests/e2e/               # E2E tests (poller smoke + toggle)
    conftest.py            # httpx fixtures (api + anon_api)
    test_smoke.py          # Health + auth smoke tests
    test_poller_enabled.py # Tests POLLER_ENABLED toggle
  Dockerfile
  requirements.txt
```

- **`services/poller/poller/`** contains core logic: SQLite dedup, Flex Web Service two-step fetch, and `poll_once()`. Notification delivery is delegated to the notifier package (see below).
- **`services/poller/poller_routes/`** contains the HTTP API for on-demand polls (`POST /ibkr/poller/run`).
- **`services/poller/poller_models.py`** is a re-export shim for shared models plus poller-specific API types (`RunPollResponse`, `HealthResponse`). The shared models (`Fill`, `Trade`, `WebhookPayloadTrades`, `WebhookPayload`) live in `services/shared/__init__.py`.

## Notifier Structure

The `services/notifier/` package is a **standalone library** (no container, no Dockerfile). It provides a pluggable notification backend system used by the poller.

```
services/notifier/
  __init__.py              # Registry, load_notifiers(), validate_notifier_env(), notify()
  base.py                  # BaseNotifier ABC (name, required_env_vars, send, default env validation)
  webhook.py               # WebhookNotifier: HMAC-SHA256 signed HTTP POST
  test_notifier.py         # Tests for registry and loader
  test_webhook.py          # Tests for webhook backend
```

- **`NOTIFIERS` env var** controls which backends are active (comma-separated, e.g. `NOTIFIERS=webhook`). Empty = no notifications (dry-run).
- **Suffix support** — `load_notifiers(suffix="_2")` reads from `TARGET_WEBHOOK_URL_2`, `WEBHOOK_SECRET_2`, etc. This powers `poller-2`.
- **Validation belongs in each notifier's `__init__`, not the coordinator.** The coordinator (`__init__.py`) is a registry + dispatcher — it must not contain backend-specific validation logic (e.g. "skip `TARGET_WEBHOOK_URL` when `DEBUG_WEBHOOK_PATH` is set"). Each `BaseNotifier` subclass validates its own env vars in its constructor and raises `SystemExit(1)` on misconfiguration. The base class provides a default validation that checks `required_env_vars()`; subclasses with custom logic (like `WebhookNotifier`'s debug-path skip) override `__init__` entirely.
- **`validate_notifier_env()`** is called by `cli/__init__.py` during pre-deploy checks. It instantiates each configured backend (triggering constructor validation) and converts `SystemExit` to a `die()` call for CLI-friendly output.
- **Adding a new backend** — create `services/notifier/<name>.py` with a class extending `BaseNotifier`, add it to `REGISTRY` in `__init__.py`. The constructor must validate all required env vars.
- **The poller calls `notify(notifiers, payload)`** — notifiers are loaded once at startup and passed through to `poll_once()`. The poller has no direct knowledge of webhook delivery mechanics.
- **Debug webhook URL resolution** — `WebhookNotifier.__init__` calls `_resolve_webhook_url(suffix)`, a pure function in `webhook.py`. If `DEBUG_WEBHOOK_PATH` is set, the URL is overridden to `http://ibkr-debug:9000/debug/webhook/{path}` (container-to-container DNS). Otherwise, it reads `TARGET_WEBHOOK_URL{suffix}`. The service name (`ibkr-debug`) and port (`9000`) are hardcoded constants in `webhook.py`. No env var mutation occurs — the resolved URL is stored in `self._url`.

## Debug Webhook Service

The `services/debug/` service is a **standalone aiohttp container** that captures webhook payloads for inspection during development and debugging.

```
services/debug/
  debug_app.py             # aiohttp app: POST/GET/DELETE /debug/webhook/{path} + GET /health
  Dockerfile               # python:3.11-slim, runs debug_app.py
  requirements.txt         # aiohttp only
  test_debug.py            # Unit tests (10 tests)
```

- **`DEBUG_WEBHOOK_PATH`** env var controls the accepted path segment. Requests to any other path return 404. When unset, the container is not running (`DEBUG_REPLICAS=0`).
- **In-memory inbox** — `_inbox: list[PayloadEntry]` stores received payloads (payload + headers + timestamp). Capped at `MAX_DEBUG_WEBHOOK_PAYLOADS` (default 100, hard max 150) with FIFO eviction.
- **Endpoints**: `POST /debug/webhook/{path}` captures a payload, `GET` returns all stored payloads, `DELETE` clears the inbox. `GET /health` returns status.
- **Logging**: Summary at INFO level, full payload+headers at DEBUG level. Set `DEBUG_LOG_LEVEL=DEBUG` in `.env` and `docker logs -f ibkr-debug` to tail payloads. Aggressive log rotation (`max-size: 10k`, `max-file: 1`) keeps disk usage minimal.
- **No auth** — the debug path in the URL acts as a shared secret. The service is not exposed to the internet unless Caddy routes to it via `debug.caddy`.
- **Port 9000** is hardcoded (`HTTP_PORT = 9000`). In production, Caddy reverse-proxies to `ibkr-debug:9000` — no host port mapping needed. Local dev uses `15003:9000` (`docker-compose.local.yml`), E2E uses `15012:9000` (`docker-compose.test.yml`).
- **Module name**: `debug_app.py` (not `main.py`) to avoid `sys.modules` collisions with `services/poller/main.py` when both are on `sys.path`.

## Dedup Structure

The `services/dedup/` package is a **standalone library** (no container, no Dockerfile). It provides SQLite dedup logic used by the poller.

```
services/dedup/
  __init__.py              # init_db(), is_processed(), mark_processed(), get_processed_ids(), mark_processed_batch(), prune()
  test_dedup.py            # Tests for dedup module
```

- **`init_db(db_path)`** creates the `processed_fills` table and returns a `sqlite3.Connection`.
- **`get_processed_ids(conn, exec_ids)`** — batch check (used by poller).
- **`mark_processed_batch(conn, exec_ids)`** — batch mark (used by poller).
- **`prune(conn, days=30)`** — delete old entries.
- **Dedup key priority** — `ibExecId → transactionId → tradeID`, resolved in `services/poller/poller/flex_parser.py` at parse time by setting `Fill.execId`. `services/shared/__init__.py::_dedup_id()` simply returns the already-resolved `fill.execId`.
- The poller has a separate metadata DB at `META_DB_PATH` (default `/data/meta.db`) on a `poller-data` volume for the timestamp watermark. The poller's `init_dedup_db()` wraps `dedup.init_db()`; `init_meta_db()` manages the metadata table independently.

## Models (Two Locations)

This project has **two model locations** — a shared source of truth and one service-specific file:

| File                               | Domain                | Contains                                                                                                                                      |
| ---------------------------------- | --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `services/shared/__init__.py`      | CommonFill (outbound) | `Fill`, `Trade`, `WebhookPayloadTrades`, `WebhookPayload`, `BuySell`, `AssetClass`, `OrderType`, `Source`, `aggregate_fills()`, `_dedup_id()` |
| `services/poller/poller_models.py` | Poller API (outbound) | Re-exports shared models + `RunPollResponse`, `HealthResponse`                                                                                |

- **`services/shared/__init__.py`** is the single source of truth for all webhook payload models.
- **Model shims only re-export models and types** (Pydantic models, enums, type aliases). Utility functions (`aggregate_fills`, `normalize_order_type`, `_dedup_id`) must be imported directly from the owning module: `from shared import aggregate_fills`. Never re-export functions through model shims.
- `poller_models.py` re-exports shared models and defines poller-specific API types. Its `SCHEMA_MODELS` contains only `[RunPollResponse, HealthResponse]`.
- `shared/__init__.py` defines `SCHEMA_MODELS = [WebhookPayloadTrades, Trade, Fill]` for the shared types.
- All external-contract models use `ConfigDict(extra="forbid")` for strict validation.

## TypeScript Types

### Namespace Convention (cross-relay standard)

All relay projects export TypeScript types using a two-tier namespace pattern:

- **`types/shared/`** → exported as the **relay's primary namespace** (named after the exchange: `Ibkr`, `Kraken`, etc.). Contains the CommonFill models (`Fill`, `Trade`, `WebhookPayloadTrades`, `WebhookPayload`, `BuySell`) generated from `services/shared/__init__.py` SCHEMA_MODELS. Every relay has this.
- **`types/<module>/`** → exported as **`<RelayName><ModuleName>`** (e.g. `IbkrPoller`). Contains service-specific types generated from that module's `SCHEMA_MODELS`. Only created when a service has unique types not in shared.

The barrel `types/index.d.ts` ties them together:

```ts
import * as Ibkr from "./shared";
import * as IbkrPoller from "./poller";
export { Ibkr, IbkrPoller };
```

A relay with no service-specific types (e.g. kraken_relay) has only the shared namespace:

```ts
export * as Kraken from "./shared";
```

### IBKR Relay Types

- Types are published as `@tradegist/ibkr-relay-types` (npm package in `types/`, not yet published).
- **Two namespaces**: `Ibkr` (shared webhook payload types) and `IbkrPoller` (poller-specific API types).
- **`make types`** regenerates both from Pydantic models:
  - `services/shared/__init__.py` → `types/shared/types.d.ts` (CommonFill models: WebhookPayloadTrades, Trade, Fill, BuySell)
  - `services/poller/poller_models.py` → `types/poller/types.d.ts` (poller-specific: RunPollResponse, HealthResponse)
- **Structure:**
  ```
  types/
    index.d.ts                 # Barrel: exports Ibkr, IbkrPoller namespaces
    package.json               # @tradegist/ibkr-relay-types
    shared/
      index.d.ts               # Re-exports: BuySell, Fill, Trade, WebhookPayloadTrades, WebhookPayload
      types.d.ts               # Generated from shared/__init__.py (SCHEMA_MODELS)
      types.schema.json         # Intermediate JSON Schema
    poller/
      index.d.ts               # Re-exports: RunPollResponse, HealthResponse
      types.d.ts               # Generated from poller/poller_models.py (SCHEMA_MODELS)
      types.schema.json         # Intermediate JSON Schema
  ```
- **Usage:** `import { Ibkr, IbkrPoller } from "@tradegist/ibkr-relay-types"`
- Each model file declares a `SCHEMA_MODELS` list at the bottom — `schema_gen.py` reads it to generate the JSON Schema. **To export a new model to TypeScript, append it to `SCHEMA_MODELS` in the relevant model shim (`poller_models.py`) or `shared/__init__.py` file and update the corresponding `types/*/index.d.ts` re-exports.**

## Code Style

- Python: `logging` module, f-strings, `aiohttp` for async HTTP in poller, `httpx` for sync HTTP client in poller.
- CLI scripts: Python (`cli/` package), invoked via `python3 -m cli <command>` or `make`. Uses only stdlib (`subprocess`, `urllib.request`, `json`, `os`). No third-party dependencies. Uses lazy dispatch (`importlib.import_module`) — each command only imports its own module.
- Terraform: all secrets marked `sensitive = true` in `variables.tf`.

## Build & Deploy

All commands available via `make` or `python3 -m cli <command>`:

```bash
make deploy    # Standalone: Terraform | Shared: rsync + compose (reads .env)
make sync      # Push .env to droplet + restart services
make sync LOCAL_FILES=1  # rsync files + rebuild + restart (full code deploy)
make destroy   # Terraform destroy
make pause     # Snapshot + delete droplet (save costs)
make resume    # Restore from snapshot
make poll      # Trigger immediate Flex poll
make e2e       # Run E2E tests (starts/stops stack)
make lint      # Run ruff linter (FIX=1 to auto-fix)
```

Direct CLI (no Make required, works on Windows):

```bash
python3 -m cli deploy
python3 -m cli sync --local-files
python3 -m cli poll 2
```

## Deployment Model (MANDATORY)

- **`make sync LOCAL_FILES=1` uses rsync** to transfer files from the local working tree to `/opt/ibkr-relay/` on the droplet. It does NOT use git on the droplet — no git clone, no deploy keys, no GitHub access needed from the server.
- **Guards:** Must be on `main` branch with a clean working tree (no uncommitted changes). This ensures rsync deploys a known committed state.
- **`--delete` flag:** rsync removes files on the droplet that no longer exist locally. This correctly handles renames and deletions but is dangerous for server-generated files.
- **Invariant: the project directory (`/opt/ibkr-relay/`) contains only source files.** No service, script, or container may write files into the project directory. All runtime-generated data (databases, caches, logs, certificates) MUST use Docker named volumes (e.g. `poller-data:/data`, `caddy-data:/data`). Docker volumes live under `/var/lib/docker/volumes/`, completely outside the project directory, and are safe from rsync `--delete`.
- **When adding new runtime data** (a new database, cache file, upload directory, etc.): create a Docker named volume in `docker-compose.yml` and mount it into the container. Never write to a path inside `/opt/ibkr-relay/`.
- **`.deployed-sha`** is the only server-side file inside the project directory. It is written by `cli/sync.py` after each `--local-files` sync and is excluded from rsync `--delete`. It records the deployed commit SHA for traceability.
- **rsync exclusions** (files never overwritten or deleted on the droplet):
  - `.git/` — not present on droplet (no git repo)
  - `.env` — pushed separately via scp (contains secrets)
  - `.env.test` — local-only test config
  - `.deployed-sha` — server-side deployment marker
  - Everything in `.gitignore` — via `--filter ':- .gitignore'`

## File Structure

```
.env.example            # Template — copy to .env and fill in real values
docker-compose.yml      # All services (caddy, poller, poller-2, ibkr-debug)
docker-compose.shared.yml # Shared-mode overlay (disables Caddy, uses SHARED_NETWORK)
docker-compose.local.yml  # Local dev override (direct port access, no TLS)
docker-compose.test.yml   # Test stack override
cli/                    # Python CLI (operator scripts)
  __init__.py           # Shared helpers (env loading, SSH, DO API, validation)
  __main__.py           # Entry point (lazy dispatch via importlib)
  core/
    deploy.py           # Standalone (Terraform) or shared (rsync + compose)
    destroy.py          # Terraform destroy
    pause.py            # Snapshot + delete droplet
    resume.py           # Restore from snapshot
    sync.py             # Push .env + restart services
  poll.py               # Trigger immediate Flex poll
  test_webhook.py       # Send test webhook payload
services/               # Business-logic services (user-facing features)
  poller/               # Flex poller service (see Poller Structure above)
    poller_models.py    # Pydantic models: Fill, Trade, WebhookPayloadTrades, WebhookPayload, BuySell, Source
  debug/                # Debug webhook inbox service (see Debug Webhook Service above)
  notifier/             # Pluggable notification backends (library, no container)
  dedup/                # SQLite dedup library (library, no container)
  shared/               # Shared models and utilities (library, no container)
infra/                  # Infrastructure backbone (no business logic)
  caddy/Caddyfile       # Reverse proxy config (uses env vars for domains)
  caddy/sites/          # Route snippets imported inside {$SITE_DOMAIN}
    ibkr.caddy          # /ibkr/* routes (poller)
    debug.caddy         # /debug/webhook/* → ibkr-debug:9000
types/                  # @tradegist/ibkr-relay-types npm package (Ibkr + IbkrPoller namespaces)
terraform/              # Infrastructure as code (DigitalOcean)
```
