# RelayPort — Project Guidelines

## Purpose

RelayPort is a **relay between broker accounts** that provides clear, common interfaces to communicate with different brokers through a single interface layer.

- **Currently supports:** IBKR (Interactive Brokers) and Kraken (crypto exchange), with more brokers planned via the relay adapter pattern
- **Currently provides:** Webhook push notifications (more notification layers planned)
- **Current direction:** Broker → User (trade fill events). Future: User → Broker (order placement)

## Code Quality (MANDATORY)

- **Always apply best practices by default.** Do not ask the user whether to follow a best practice — just do it. Use idiomatic Python naming, file organization, and patterns. When there is a clearly better approach (naming, structure, error handling), use it directly and explain why.
- **No unused imports.** After writing or editing any Python file, verify every `import` is actually used in the file. Remove any that are not. This applies to new files and edits to existing files alike.
- **No `__all__`.** All imports are explicit (`from module import X`). `__all__` only controls star-imports, which we never use.
- **No `assert` for runtime guards.** `assert` is stripped under `python -O`, turning invariant checks into silent `None`/`AttributeError`. Use `if ... raise RuntimeError(...)` (or `die()`) for any check that must hold at runtime.
- **Makefile must mirror CLI arguments.** When adding a new parameter to a `cli/` command, always add the corresponding `$(if $(VAR),--flag $(VAR))` to the Makefile target so `make <target> VAR=value` works. **CLI parameters that are optional in the Makefile must be named flags (`--currency`, `--exchange`), never positional args.** When the Makefile uses `$(if $(VAR),...)`, omitting `VAR` omits the entire argument — if the CLI parameter is positional, downstream args shift into the wrong position and get silently misparsed.
- **Update README.md when changing public interfaces.** When adding or modifying CLI commands, Makefile targets, API endpoints, or env vars, always update the README to reflect the change.
- **Run `make lint` after every code change.** Ruff enforces unused imports (F401), import ordering (I001), unused variables, common pitfalls (bugbear), and modern Python idioms. If ruff fails, fix before committing. Use `make lint FIX=1` to auto-fix safe issues (import sorting, etc.).
- **Register new modules in `pyproject.toml`.** When adding a new Python service, package, or standalone module under `services/` or `types/python/`, immediately add it to `pyproject.toml`: (1) `tool.pytest.ini_options.testpaths` (if it has tests), (2) `tool.ruff.src`, (3) `tool.ruff.lint.isort.known-first-party`, and (4) the mypy invocation in the Makefile. Also add it to the ruff and mypy paths in the Makefile `lint:` and `typecheck:` targets. Missing any of these causes silent miscategorisation (isort), missed tests (pytest), or unchecked code (mypy).
- **Centralise env var reads into typed getter functions.** Each env var must be read in exactly one place — a getter function in the module that owns it (e.g. `_get_flex_token()` in `relays/ibkr/__init__.py`). The getter applies `.strip()` and any type conversion (`int()`, boolean parsing). All other code — including other modules, `main.py` entrypoints, and route handlers — imports and calls the getter. Never call `os.environ.get()` inline except inside a getter. This eliminates duplicated reads, inconsistent `.strip()`, and scattered default values.
- **Getters must validate and fail fast.** Every getter that reads an env var must validate the value and raise `SystemExit` with a descriptive message on invalid input — never let a bad value propagate silently. For required string vars (no default), check emptiness: `if not val: raise SystemExit("IBKR_FLEX_TOKEN must be set")`. For `int()` conversions, wrap in `try/except ValueError: raise SystemExit(f"Invalid VAR={raw!r} — must be an integer")`. For boolean flags, parse inside the getter (`flag not in ("0", "false", "no")`) and return `bool`. Callers should never need to validate a getter's return value — if the getter returns, the value is valid.
- **Prefer pure functions over side-effect functions.** Never write an `apply_*()` / `set_*()` function that silently mutates system state (env vars, globals, module-level caches) as its primary purpose. Instead, compute and return the value — let the caller decide how to use it. For example, instead of `apply_debug_url_override()` that mutates `os.environ`, write a `resolve_url()` that returns the URL and let the consumer store it. If a side-effect function is truly unavoidable (e.g. one-time DB migration), add an inline comment at every call site explaining **what** is mutated and **why**: `# Mutates os.environ["X"] to enable Y`.
- **Never bulk-set `os.environ` with empty-string fallbacks.** A loop like `os.environ[key] = env(name, "")` silently overrides downstream defaults (e.g. Terraform `variable` defaults, library config) with empty strings — the downstream system sees the variable as _set but empty_ instead of _unset_, which breaks `tonumber()`, validation blocks, and non-string parsing. When bridging env vars to another system (Terraform `TF_VAR_*`, subprocess env, etc.), only export a key when the source value is present and non-empty. Explicitly `os.environ.pop(key, None)` otherwise so stale values from a previous run don't leak through.

## Security Rules (MANDATORY)

- **No hardcoded credentials** — passwords, API tokens, secrets, and keys MUST come from environment variables (`.env` file or `TF_VAR_*`). Never write real values in source files.
- **No hardcoded IPs** — use `DROPLET_IP` from `.env.droplet`. In documentation, use `1.2.3.4` as placeholder.
- **No hardcoded domains** — use `example.com` variants (`trade.example.com`) in docs and code. Actual domains are loaded at runtime via `SITE_DOMAIN` env var.
- **No email addresses or personal info** — never write real names, emails, or account IDs in committed files. Use `UXXXXXXX` for IBKR account examples.
- **No developer-machine paths** — never write absolute paths like `/Users/john/...` or `C:\Users\john\...` in any committed file (docs, instructions, configs, comments). These leak personal and machine-specific information into a public repo. Reference sibling projects by name only, never by local filesystem path.
- **No logging of secrets or sensitive operational data** — never `log.info()` or `print()` tokens, passwords, or API keys. Log actions and outcomes, not credential values. When adding any `log.info()` or `log.debug()` call, check whether the logged value contains sensitive fields (e.g. `accountId`, `acctAlias`, account numbers, IPs, domains). Never log full model dumps at `info` level — use `log.debug` with explicit field exclusion: `log.debug("Trade: %s", trade.model_dump_json(exclude={"accountId", "acctAlias"}))`. Prefer logging counts, symbols, and statuses over full objects.
- **`.env`, `.env.droplet`, `.env.relays`, `*.tfvars`, and `.env.test` are gitignored** — never commit them. Use `env_examples/` templates with placeholder values as reference.
- **Raw Flex XML dumps must never be committed.** Live Flex responses contain real account IDs, execution IDs, and order IDs. Always sanitize via `make ibkr-flex-refresh` (or `fixtures/sanitize.py` directly) before committing any Flex XML. The intermediate raw file (`fixtures/raw.xml`) is gitignored. Only the sanitized fixtures (`activity_flex_sample.xml`, `trade_confirm_sample.xml`) are committed.
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
- After modifying any model in `services/shared/models.py`, `services/relay_core/notifier/models.py`, or `services/relay_core/relay_models.py`, also run `make types` to regenerate the TypeScript and Python type definitions.
- **Always verify type safety by breaking it first.** After any refactor that touches types or model construction, deliberately introduce a type error (e.g. pass a `str` where `float` is expected), run `make typecheck`, and confirm it **fails**. Then revert and confirm it passes. Never assume mypy catches something — prove it.
- **Avoid `dict[str, Any]` round-trips.** Never use `model_dump()` → `dict` → `Model(**data)` — mypy cannot type-check `**dict[str, Any]`. Use explicit keyword arguments or `model_copy(update=...)` instead.
- **Prefer strict `Literal` types over bare `str` on Pydantic models.** Financial applications demand precision — a `str` field silently accepts typos and invalid values. When a field has a known set of valid values (e.g. `BuySell`, `OrderType`, `AssetClass`), always use the existing `Literal` type. Only fall back to `str` when the external source (e.g. Flex XML) genuinely returns unbounded values — and document why with an inline comment.
- **No `# type: ignore` without justification.** Do not bypass the type checker. Fix the root cause instead — use proper type annotations, import the correct type, widen a dict annotation, or use `cast()`. If suppression is truly unavoidable (e.g. untyped third-party library), the comment must include a reason: `# type: ignore[attr-defined] # ib_async.Foo has no stubs`. A bare `# type: ignore` with no explanation is never acceptable.
- **Use `cast()` instead of `# type: ignore[arg-type]`.** When passing a mock or compatible object where mypy expects a concrete type, use `cast(TargetType, mock)` — not `# type: ignore[arg-type]`. This applies everywhere: test code, adapters, and third-party library wrappers. `cast()` is a documented assertion that preserves type-checking downstream; `# type: ignore` silently disables it.
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

- **Mark-after-notify, never before.** `mark_processed_batch()` must only run AFTER `notify()` completes successfully. A crash between mark and notify silently drops fills — the fill is recorded as processed but the webhook was never sent. The dedup logic skips it on the next cycle, so the fill is never retried. This is an unrecoverable data loss.
- **The correct pattern:** run `notify()` and `mark_processed_batch()` sequentially in the same execution context (same thread or same `asyncio.to_thread` call). If `notify()` raises, the fill remains unprocessed and will be retried on the next cycle.
- **Never separate mark from notify with an `await` boundary.** In async code, an `await` between mark and notify allows the process to crash between the two operations. Keep them atomic within a single synchronous block (e.g. inside `asyncio.to_thread`).
- **Replay mode is the exception.** `poll --replay N` intentionally skips dedup — it resends the last N fills without marking them. This is by design for debugging/recovery.
- **SQLite commits must be explicit.** After any `INSERT`/`UPDATE` to SQLite (dedup DB or metadata DB), call `conn.commit()` immediately. Without an explicit commit, a crash loses the write silently. Never rely on implicit commit behavior.

## Concurrency Safety (MANDATORY)

- **Assume concurrency by default.** The relay is async (aiohttp). Any handler can be interrupted at an `await`. When writing new code, always consider what happens if two requests arrive at the same time.
- **Always be wary of race conditions.** Before merging any code that touches shared state, ask: "Can two callers interleave here? What breaks if they do?"
- **Never use TOCTOU (Time of Check, Time of Use) patterns with locks.** Do NOT check `lock.locked()` and then `async with lock:` — another coroutine can acquire the lock between the check and the acquisition, defeating the guard. This is a race condition.
- **Lock acquisition must BE the check.** Use `asyncio.wait_for(lock.acquire(), timeout=0)` with `try/finally: lock.release()` to fail-fast, or accept that `async with lock:` will queue. Never separate "is it locked?" from "acquire it."
- **This applies to all shared-state guards** — locks, database transactions, file locks, semaphores, balance checks. If the action is "check a condition, then act on it," both steps must be atomic.
- **Never share a `sqlite3.Connection` across threads.** `sqlite3.Connection` is not thread-safe. When using `asyncio.to_thread()`, either pass the connection into a single synchronous function that does all DB work in one thread, or use an `asyncio.Lock` to ensure only one `to_thread()` call uses the connection at a time. Never allow two concurrent `to_thread()` calls to touch the same connection — this causes intermittent `OperationalError` and data corruption.
- **Poller engine `to_thread` pattern: create connections inside the worker thread.** Do NOT create `sqlite3.Connection` on the main (event-loop) thread and pass it into `asyncio.to_thread(poll_once, conn, ...)` — even with `check_same_thread=False`, this is cross-thread use and unsafe. Instead, `poll_once()` creates thread-local connections internally (via `init_dedup_db()` / `init_meta_db()`), closing them in a `finally` block. The caller (`_poll_loop`, `handle_poll`) passes only non-DB arguments. This ensures every `to_thread` call uses connections that were both created and closed on the same worker thread.
- **Financial operations require extra scrutiny.** Any code path that places orders, moves money, or modifies account state must be reviewed for: race conditions, double-execution, partial failure (what if it crashes between two steps?), and idempotency.
- **Use `asyncio.get_running_loop()`, never `asyncio.get_event_loop()`.** `get_event_loop()` is deprecated since Python 3.10 for contexts without a running loop and emits `DeprecationWarning` in 3.12+. Code that calls `loop.call_later()`, `loop.create_task()`, etc. always runs on the event-loop thread, so `get_running_loop()` is correct, explicit, and raises `RuntimeError` immediately if accidentally called off-loop.

## Local Development

- **`.venv` is the project's virtual environment.** Created by `make setup` using Homebrew Python. All dev dependencies are installed there.
- **Auto-activation** is configured in `~/.zshrc` via a `chpwd` hook — the venv activates automatically when `cd`'ing into the project directory.
- **`make setup`** creates the `.venv` (if missing), installs all dependencies (`requirements-dev.txt` + `services/relay_core/requirements.txt`), writes a `.pth` file, and copies `env_examples/*` → `.<name>` (e.g. `env_examples/env` → `.env`) for any missing env files. It also auto-heals a broken `.venv` (e.g. after a Python upgrade moves the interpreter) by detecting a missing `pip` import and rebuilding the venv from scratch before installing.
- **`relayport.pth`** is created inside `.venv/lib/pythonX.Y/site-packages/` by `make setup`. It adds `services/debug/`, `services/`, and `services/relay_core/` to `sys.path` so that `from relay_core import ...`, `from relay_core.dedup import ...`, `from relay_core.notifier import ...`, `from debug_app import ...`, and `from shared import ...` work everywhere (CLI, tests, scripts) without `sys.path` hacks or `PYTHONPATH`.
- **`.venv/` is gitignored** — never commit it.
- **`docker-compose.local.yml` adds bind mounts** that shadow the `COPY`'d files in the image with your local source tree (`:ro`). This means code changes are visible on container restart — no rebuild needed. `make local-up` builds the images once; after that, `make sync` (when `DEFAULT_CLI_ENV=local`) just restarts containers.
- **`make sync` respects `DEFAULT_CLI_ENV`.** When set to `local`, `make sync` restarts the local compose stack. When `prod` (default), it runs the full CLI sync to the droplet. Override per-command with `ENV=local` or `ENV=prod`.
- **`make logs` also respects `DEFAULT_CLI_ENV`.** `make logs S=debug` streams local container logs when local, droplet logs when prod.

## Dependency Management

- **Runtime deps (`services/relay_core/requirements.txt`)** use exact pins (`==`). These are deployed to production containers — builds must be reproducible.
- **`requirements-dev.txt` contains only dev-only tools** (mypy, pytest, ruff). Runtime deps (`pydantic`, `httpx`, etc.) belong exclusively in `services/relay_core/requirements.txt`. Both files are always installed together (CI and `make setup`), so runtime deps are available in the dev environment without duplication. Never add a runtime dep to `requirements-dev.txt` — it would create two separate Dependabot PRs for the same package with no way to combine them.
- **When adding a new dependency**, always pin it immediately — never leave it unpinned. Runtime deps go in the service's `requirements.txt` with an exact pin (`==`); dev-only tools go in `requirements-dev.txt` with a major-version constraint (`>=X,<X+1`).
- **All services pinning the same dependency must use the same version.** When multiple `requirements.txt` files pin the same package (e.g. `aiohttp`), keep versions aligned. Check existing pins with `grep -r 'aiohttp==' services/*/requirements.txt` before adding a new one.

## Environment Files

Configuration is split into three env files to separate concerns and enable scalable relay configuration:

- **`.env`** — App-level config: `SITE_DOMAIN`, `API_TOKEN`, `NOTIFIERS`, `RELAYS`, `POLL_INTERVAL`, listener settings. Injected into the `relays` container via `env_file:` in `docker-compose.yml`. Pushed to the droplet by `make sync` / `make deploy`.
- **`.env.relays`** — Relay-prefixed env vars: `IBKR_FLEX_TOKEN`, `IBKR_FLEX_QUERY_ID`, relay-specific overrides (`IBKR_NOTIFIERS`, `IBKR_TARGET_WEBHOOK_URL`). Also injected via `env_file:` (marked `required: false` so the stack starts even without it). Adding a new relay's vars requires no compose changes — just add the prefixed vars.
- **`.env.droplet`** — Developer-machine-only vars that are never pushed to the droplet or injected into containers. The name reflects its origin (droplet infrastructure config) but its scope is broader: any var that belongs on the developer's machine rather than the server lives here. Currently: `DEPLOY_MODE`, `DO_API_TOKEN`, `DROPLET_IP`, `SSH_KEY`, `DROPLET_SIZE`, `DEFAULT_CLI_ENV`. Only read by `cli/` commands and the Makefile.
- **`.env.test`** — E2E test config. Used only in `docker-compose.test.yml` via `env_file: !override`.
- **Templates** live in `env_examples/` (gitignored names: `env`, `env.droplet`, `env.relays`, `env.test`). `make setup` copies them to `.<name>` if missing.

## Docker

- **`env_file:` is the correct pattern for the `relays` service.** The base `docker-compose.yml` declares `env_file: [.env, path: .env.relays, required: false]` on the `relays` service. This injects all app-level and relay-specific vars without enumerating each one in the `environment:` block. Only guards (`API_TOKEN: ${API_TOKEN:?...}`) and vars with compose-level defaults (`POLL_INTERVAL: ${POLL_INTERVAL:-600}`) appear in `environment:`.
- **Test isolation uses `env_file: !override`.** `docker-compose.test.yml` overrides the base env_file list with `env_file: !override` followed by `- .env.test`, replacing (not appending to) the production env files. Test-specific values are hardcoded in the `environment:` block. This keeps test and production environments fully isolated.
- **`DEBUG_WEBHOOK_PATH`** enables the `debug` container. When set (non-empty) in `.env`, the compose default `${DEBUG_REPLICAS:-0}` is overridden to `1` by the CLI's `_compose_env()`. The debug service has aggressive log rotation (`max-size: 10k`, `max-file: 1`) since its sole purpose is transient payload inspection. Set `DEBUG_LOG_LEVEL=DEBUG` in `.env` to include full payload+headers in `docker logs`.
- **`.dockerignore` uses an allowlist** (`*` to exclude everything, then `!services/<module>/**` for each module). Tests, `__pycache__`, and Dockerfiles are re-excluded. This means adding new source files within an existing module requires **no** `.dockerignore` changes.
- **When adding a new standalone module** (e.g. `services/foo/`), you must add `!services/foo/**` plus test/pycache exclusions to `.dockerignore` — the allowlist excludes everything by default. Also add the corresponding `COPY` to the Dockerfile. Without this, the build context won't include the new module.
- The relay_core Dockerfile uses directory COPYs (`COPY services/relay_core/ ./relay_core/`, `COPY services/relays/ ./relays/`) so new files are picked up automatically.

## Architecture

Three Docker containers in a single Compose stack on a DigitalOcean droplet (debug is optional):

| Service  | Role                                                                                                                                                                        |
| -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `caddy`  | Reverse proxy with automatic HTTPS (Let's Encrypt)                                                                                                                          |
| `relays` | Multi-relay service: loads broker adapters via the registry, runs pollers + listeners + HTTP API. Disabled when `RELAYS` is empty (API server still runs for health checks) |
| `debug`  | Debug webhook inbox — captures webhook payloads for inspection. Disabled by default (`DEBUG_REPLICAS=0`), enabled when `DEBUG_WEBHOOK_PATH` is set                          |

### Relay Registry Pattern

The `relays` container uses a **registry pattern** to support multiple broker adapters:

1. `RELAYS` env var lists active relays (e.g. `RELAYS=ibkr`, `RELAYS=ibkr,kraken`).
2. `registry.py` validates each name against `RelayName` (a `Literal` type in `shared/models.py`).
3. For each relay, the registry dynamically imports `relays.<name>` and calls `build_relay()`.
4. The adapter returns a `BrokerRelay` dataclass with `PollerConfig`s, `ListenerConfig`, and notifiers.
5. `main.py` starts a poll loop per `PollerConfig` and a WS listener (if configured).

**Adding a new relay adapter — step-by-step:**

Use the existing `ibkr` and `kraken` relays as reference implementations. IBKR demonstrates a complex adapter (XML polling + bridge WS with two event types), while Kraken demonstrates a simpler adapter (JSON REST polling + native WS with token-based auth).

1. **Update shared types** (`services/shared/models.py`):
   - Add the relay name to `RelayName` (e.g. `Literal["ibkr", "kraken", "newbroker"]`).
   - Add any new source identifiers to `Source` (e.g. `"newbroker_rest"`, `"newbroker_ws"`).

2. **Create the relay adapter package** (`services/relays/<name>/`):
   - `__init__.py` — must export `build_relay(notifiers: list[BaseNotifier]) -> BrokerRelay`. This is the only contract the registry requires.
   - Add broker-specific TypedDicts for raw API shapes (e.g. `<name>_types.py`).
   - Add a REST client if the broker has a REST API (e.g. `rest_client.py`).
   - Add a WS parser if the broker has a WebSocket API (e.g. `ws_parser.py`).

3. **Implement `build_relay()`** — it must return a `BrokerRelay` with:
   - `name`: the relay name (must match `RelayName`).
   - `notifiers`: pass through from the argument.
   - `poller_configs`: list of `PollerConfig` (can be empty if listener-only). Each needs:
     - `fetch: Callable[[], str | None]` — returns raw data (JSON string, XML, etc.) or None on failure.
     - `parse: Callable[[str], tuple[list[Fill], list[str]]]` — parses raw data into (fills, errors).
     - `interval: int` — poll interval in seconds.
   - `listener_config`: a `ListenerConfig` or `None` (can be None if poller-only). Needs:
     - `connect: Callable[[aiohttp.ClientSession], Awaitable[aiohttp.ClientWebSocketResponse]]` — async callback that connects, authenticates, subscribes, and returns a ready-to-read websocket. The engine handles reconnection with exponential backoff; this callback is called on each reconnect.
     - `on_message: Callable[[dict], Awaitable[list[OnMessageResult]]]` — parses a WS JSON dict into a list of `OnMessageResult`. Each result has `fill` (or None to skip) and `mark` (True for dedup+notify+mark pipeline, False for fire-and-forget).
     - `event_filter: Callable[[dict], bool]` — return True for events that should reach `on_message`, False to skip (heartbeats, subscription acks, etc.).
     - `debounce_ms: int` — optional debounce buffer (0 = disabled).

4. **Environment variables** — follow the prefix convention:
   - Use `{RELAY}_` prefix for all relay-specific vars (e.g. `KRAKEN_API_KEY`).
   - Use `relay_core.env.get_env()` / `get_env_int()` for vars that support prefix fallback.
   - Use direct `os.environ.get()` wrapped in getter functions for broker-specific vars with no generic equivalent.
   - Add the vars to `env_examples/env.relays` (the template file) — this is **mandatory**. Follow the existing relay sections as a model: uncomment required vars, comment out optional ones with their defaults. Document in the README.
   - Update the `RELAYS` comment in `env_examples/env` to include the new relay name in the "Available relays" list.

5. **Register the module**:
   - `pyproject.toml`: add to `tool.pytest.ini_options.testpaths`, `tool.ruff.src`, `tool.ruff.lint.isort.known-first-party`.
   - Makefile: add to `lint:` and `typecheck:` targets.
   - `.dockerignore`: add `!services/relays/<name>/**` if needed (currently `!services/relays/**` covers all relay packages).

6. **Timestamp normalisation** — every `Fill.timestamp` must be in the canonical form `YYYY-MM-DDTHH:MM:SS` (UTC, no `Z`, no fractional seconds). See the [Timestamp normalisation convention](#timestamp-normalisation-convention-apply-to-all-relay-adapters) section below. If the broker's native timestamp format is not ISO-8601, add a `services/relays/<name>/timestamps.py` with a small `<format>_to_iso(raw) -> str` helper that validates and converts. The Flex/bridge parsers chain it as `normalize_timestamp(<format>_to_iso(raw), assume_tz=tz)`. **Never add broker format knowledge to `services/shared/time_format.py`** — that module must stay broker-agnostic.

7. **Option contracts** — if the broker supports option derivatives, populate `Fill.option` (type `OptionContract`) when `assetClass == "option"`, and leave it `None` for all other instruments. `OptionContract` fields:
   - `rootSymbol: str` — the underlying ticker (e.g. `"AVGO"`). For IBKR, this is `contract.symbol` (not `contract.localSymbol`).
   - `strike: float` — strike price.
   - `expiryDate: str` — expiry in ISO `YYYY-MM-DD` form. Use `flex_date_to_iso()` (or a broker-equivalent) to convert compact dates.
   - `type: Literal["call", "put"]` — derived from the broker's put/call indicator.
   For IBKR: `Fill.symbol = contract.localSymbol` with spaces stripped (OCC ticker, e.g. `"AVGO260620C00200000"`) and `option.rootSymbol = contract.symbol` (underlying, e.g. `"AVGO"`). IBKR pads the underlying to 6 characters with spaces in the OCC format — always `.replace(" ", "")` so the symbol is URL-friendly.
   **Never emit a fill with `assetClass == "option"` when option metadata is missing or invalid** — skip the row and surface a parse error instead. An incomplete `option` object is worse than a missing fill.

8. **Write tests** — colocate unit tests next to the source files (e.g. `test_<name>.py`). If you added a `timestamps.py`, add a `test_timestamps.py` with positive + negative cases (the point of the helper is to reject typos that `datetime.fromisoformat` would silently accept).

9. **Update README** — add the relay's env vars, webhook payload examples, and any broker-specific setup instructions.

10. **Verify** — `make test`, `make typecheck`, `make lint` must all pass.

### Env file flow

```
.env         ─┐
.env.relays  ─┤── env_file: in docker-compose.yml ──▶ relays container
              │
.env.droplet ─── CLI only (never pushed to container)
.env.test    ─── env_file: !override in docker-compose.test.yml ──▶ test containers
```

All secrets are injected via `env_file:` in `docker-compose.yml`.
Caddy reads `SITE_DOMAIN` from its `environment:` block — the Caddyfile uses `{$SITE_DOMAIN}` syntax.

### Caddy Snippet Structure

The Caddyfile uses `import` directives to compose routing from snippet files:

```
infra/caddy/
  Caddyfile              # Shell: imports from sites/ and shared dirs
  sites/
    ibkr.caddy           # SITE_DOMAIN route handlers (handle /relays/*)
    debug.caddy          # Debug webhook routes (handle /debug/webhook/*)
```

Shared projects deploy snippets to `/opt/caddy-shared/sites/` on the droplet (not into the host project's directory). The host Caddy mounts both:

- `./infra/caddy/sites/` → `/etc/caddy/sites/` (host project's own routes)
- `/opt/caddy-shared/sites/` → `/etc/caddy/shared-sites/` (shared projects' routes)

During shared deploy, snippet files are **templated** — all `{$VAR}` placeholders are replaced with literal env var values from the shared project's `.env`. This avoids requiring the host Caddy container to have the shared project's env vars.

- **`sites/*.caddy`** contain `handle` blocks imported inside the `{$SITE_DOMAIN}` site definition. Each project writes one snippet. Routes must be prefixed to avoid collisions. The `debug.caddy` snippet routes `/debug/webhook/*` to the `debug` container.
- This structure allows multiple projects to share a single Caddy instance on the same droplet.

## Sibling Project: ibkr_bridge

This project (`relayport`) and its sibling project `ibkr_bridge` share the same CLI deploy/destroy/sync infrastructure pattern. **Any change to `cli/core/deploy.py`, `cli/core/destroy.py`, or `cli/core/sync.py` in this project must be mirrored in the sibling project, and vice versa.** This includes: Terraform state management, reserved IP handling, rsync exclusions, env file push logic, and compose startup commands. When you modify CLI core logic here, explicitly remind the user to apply the equivalent change to `ibkr_bridge`, and offer to do it in the same session.

## Deployment Modes

The deployment mode is controlled by `DEPLOY_MODE` in `.env.droplet` (required, validated before any deploy or sync).

### Standalone Mode (`DEPLOY_MODE=standalone`)

- Set `DO_API_TOKEN` in `.env.droplet`. `make deploy` runs Terraform to create a new droplet, firewall, and reserved IP, then the CLI rsyncs project files, pushes `.env` + `.env.relays`, and runs `docker compose up -d --build`.
- Terraform only creates infrastructure — cloud-init installs Docker and creates the project directory. The CLI handles all file transfer and service startup.
- After deploy, add `DROPLET_IP` from terraform output to `.env.droplet` for `make sync`.
- `DO_API_TOKEN` can be removed after first deploy for security — the mode is determined by `DEPLOY_MODE`, not by token presence.

### Shared Mode (`DEPLOY_MODE=shared`)

- Set `DROPLET_IP` and `SSH_KEY` in `.env.droplet` (no `DO_API_TOKEN` needed).
- `make deploy` rsyncs files, pushes `.env` + `.env.relays`, and starts services using `docker-compose.shared.yml` overlay.
- The shared overlay disables Caddy (the host project runs it) and connects all containers to the shared Docker network (`SHARED_NETWORK` env var, typically `relay-net`).
- **`SHARED_NETWORK` controls cross-project networking.** The base `docker-compose.yml` uses `name: ${SHARED_NETWORK:-}` for the default network. When unset, Docker Compose creates a project-scoped network (isolated). When set to the same value across projects (e.g. `relay-net`), all projects share a single network and can reach each other's containers by service name. The shared overlay (`docker-compose.shared.yml`) sets the network to `external: true`, which merges on top of the base definition.
- Caddy snippet files must be deployed to the host project's Caddy to enable routing.
- `make sync` uses the shared compose overlay automatically.

## Droplet Sizing

- **`DROPLET_SIZE`** sets the DigitalOcean droplet slug directly (e.g. `s-1vcpu-512mb`). This is a lightweight relay deployment — no JVM heap sizing needed.
- `cli/__init__.py` `_droplet_size()` reads `DROPLET_SIZE`.
- `cli/core/resume.py` uses `cfg.droplet_size()` which delegates to the same `_droplet_size()` function.

## Auth Pattern

- API endpoints under `/relays/*` require `Authorization: Bearer <API_TOKEN>` (HMAC-safe comparison via `hmac.compare_digest`).
- **All authenticated routes must use the `AUTH_PREFIX` constant** (from `relay_core.routes.middlewares`) when registering with the router. The auth middleware uses the same constant to decide which requests require a token — hardcoding the path in either place causes them to drift out of sync.
- Webhook payloads are signed with HMAC-SHA256 (`X-Signature-256` header) via the notifier package.

## E2E Testing

- **E2E tests run against a local Docker stack** defined by `docker-compose.test.yml` (relays + debug, no Caddy).
- **Credentials live in `.env.test`** (gitignored). Template: `env_examples/env.test`.
- **`make e2e`** starts the stack, runs pytest, then tears down. Always cleans up, even on test failure.
- **`make e2e-up` / `make e2e-down`** for manual stack management during debugging.
- **`make e2e-run`** restarts `relays` and `debug` containers (to pick up code changes from volume mounts), then runs the E2E tests. Safe to call repeatedly during development — no need to rebuild or restart manually.
- **Test relays service runs on `localhost:15011`** with hardcoded token `test-token`.

### E2E Conftest Pattern

The E2E conftest (`services/relay_core/tests/e2e/conftest.py`) uses a **two-tier preflight** pattern:

- **`_stack_preflight`** — `scope="session"`, `autouse=True`. Hits `/health` on the relays service. Calls `pytest.exit()` if the stack is unreachable (hard failure — no tests run).
- **`_bridge_preflight`** — `scope="session"`, on-demand (requested by listener tests via `bridge_api`). Checks `LISTENER_ENABLED`, bridge credentials, and bridge reachability. Calls `pytest.skip()` if any prerequisite is missing (soft skip — other tests still run).

### Listener E2E Tests

- **Listener E2E tests are opt-in** — they require a running ibkr_bridge local stack and `LISTENER_ENABLED=true` in `.env.test`.
- **Preflight skip logic**: tests skip (not fail) when `LISTENER_ENABLED` is not set, bridge credentials are missing, or the bridge is unreachable.
- **E2E conftest loads `.env.test` directly** using a stdlib `_load_env_test()` helper (key=value parser, no `python-dotenv` dependency).
- **Required `.env.test` vars**: `IBKR_BRIDGE_WS_URL`, `IBKR_BRIDGE_API_BASE_URL`, `IBKR_BRIDGE_API_TOKEN`.

## Test File Convention

- **Unit tests are colocated** next to the source file they test: `flex_parser.py` → `test_flex_parser.py`, `registry.py` → `test_registry.py`.
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
- **E2E conftest fixtures must use `yield` with a context manager.** Never `return httpx.Client(...)` — the client is never closed and leaks sockets. Use `with httpx.Client(...) as client: yield client` instead. Scope to `session` (one client per test run).

## Relay Core Structure

The `services/relay_core/` service is the main Docker container. It provides the generic polling engine, listener engine, HTTP API, and relay registry.

```
services/relay_core/
  main.py                  # Entrypoint — loads relays, starts pollers + listeners + API
  __init__.py              # BrokerRelay dataclass, re-exports engine types (PollerConfig, ListenerConfig, etc.)
  context.py               # Relay context singleton: init_relays(), get_relay(), get_relays()
  env.py                   # Shared env var helpers: get_env(), get_env_int() with prefix/suffix fallback
  registry.py              # Relay registry — loads adapters from RELAYS env var
  poller_engine.py         # Generic poller: init_dedup_db, init_meta_db, poll_once, prune_old
  listener_engine.py       # Generic WS listener: connect, dedup, notify, reconnect with backoff
  relay_models.py          # Re-export shim: notifier payload contracts + relay API types (RunPollResponse, HealthResponse)
  dedup/                   # SQLite dedup library
    __init__.py            # init_db(), is_processed(), mark_processed(), prune()
  notifier/                # Pluggable notification backends
    __init__.py            # Registry, load_notifiers(), validate_notifier_env(), notify()
    base.py                # BaseNotifier ABC
    models.py              # Outbound payload contracts (WebhookPayloadTrades, WebhookPayload)
    webhook.py             # WebhookNotifier: HMAC-SHA256 signed HTTP POST
  routes/                  # HTTP API
    __init__.py            # Orchestrator: create_app(), start_api_server(), handle_health, handle_poll
    middlewares.py         # Auth middleware (Bearer token, AUTH_PREFIX=/relays)
  tests/e2e/               # E2E tests (smoke + listener)
    conftest.py            # httpx fixtures + two-tier preflight
  Dockerfile
  requirements.txt
```

- **`services/relay_core/main.py`** reads `RELAYS`, loads adapters via the registry, initialises the relay context (`init_relays()`), starts the HTTP API, then spawns a poll loop per `PollerConfig` and a WS listener per relay (if configured). When `RELAYS` is empty, the API server starts alone (for health checks).
- **`services/relay_core/context.py`** provides the relay context singleton. `init_relays(relays)` is called once at startup by `amain()`, then `get_relay(name)` and `get_relays()` are available anywhere to access relay config (notifiers, retry config, poller/listener configs) without parameter threading. `_reset()` is exposed for test teardown. Uses `TYPE_CHECKING` guard for `BrokerRelay` import to avoid circular import with `__init__.py`.
- **`services/relay_core/poller_engine.py`** provides `poll_once(relay_name, poller_index)` — the generic polling function. Resolves `PollerConfig`, notifiers, and retry config from the relay context. Handles dedup, aggregation, notify, and mark. Broker-specific logic (Flex fetch, XML parsing) lives in the relay adapter.
- **`services/relay_core/listener_engine.py`** provides `start_listener(relay_name)` — the generic WS listener. Resolves `ListenerConfig`, notifiers, and retry config from the relay context. Calls the adapter's `connect` callback to obtain a connected websocket, dispatches events via `event_filter` and `on_message` callbacks, handles dedup + notify + mark, and auto-reconnects with exponential backoff. The `connect` callback owns the entire connection protocol (auth, subscription) — the engine only manages the message loop and reconnection.
- **`services/relay_core/relay_models.py`** is a re-export shim for the notifier payload contracts plus relay-specific API types (`RunPollResponse`, `HealthResponse`). Listed in `schema_gen.py:SCHEMA_MODELS` under key `"relay_core.relay_models"`.
- **`services/relay_core/routes/__init__.py`** provides `GET /health` (unauthenticated) and `POST /relays/{relay_name}/poll/{poll_idx}` (authenticated, 1-based index).
- **`services/relay_core/env.py`** provides `get_env(var, prefix, suffix, default)` and `get_env_int(var, prefix, suffix, default)` — shared helpers for reading env vars with relay-specific prefix fallback. Resolution order: `{prefix}{var}{suffix}` → `{var}{suffix}` → `default`. All relay-core env var readers (`get_poll_interval`, `get_debounce_ms`, `load_retry_config`, notifier env loading) use these helpers. When adding new env var readers, use `get_env` / `get_env_int` from `relay_core.env` instead of writing inline `os.environ.get()` with manual fallback logic.

## Relay Adapter Structure

Each broker adapter lives under `services/relays/<name>/`. The adapter is a small package that wires broker-specific logic into the generic `relay_core` engines. The only required contract is `build_relay(notifiers: list[BaseNotifier]) -> BrokerRelay`.

```
services/relays/ibkr/
  __init__.py              # build_relay() → BrokerRelay, env getters, map_fill(), IBKR-specific logic
  bridge_models.py         # Mirrored WsEnvelope + related types from ibkr_bridge
  flex_fetch.py            # Flex Web Service two-step fetch (pure library, no CLI code)
  flex_dump.py             # CLI entrypoint: fetch + write to disk (invoked by make ibkr-flex-dump/refresh)
  flex_parser.py           # Flex XML parser (Activity + Trade Confirmation)
  fixtures/
    sanitize.py            # Sanitize a raw Flex dump into a committable fixture
    activity_flex_sample.xml    # Sanitized Activity Flex fixture (committed, no real IDs)
    trade_confirm_sample.xml    # Sanitized Trade Confirmation fixture (committed, no real IDs)
  test_flex_parser.py      # Tests for flex_parser
  test_flex_fetch.py       # Tests for flex_fetch (RedactTokenFilter, fetch error paths)

services/relays/kraken/
  __init__.py              # build_relay() → BrokerRelay, env getters, REST poller + WS listener adapters
  rest_client.py           # KrakenClient: HMAC-SHA512 auth, get_trades_history(), get_ws_token()
  ws_parser.py             # WS v2 executions channel parser → list[Fill]
  kraken_types.py          # TypedDicts for raw Kraken API shapes (WS + REST)
```

### IBKR adapter

- **`build_relay(notifiers)`** constructs a `BrokerRelay` with IBKR-specific `PollerConfig`s (Flex fetch + parse callbacks) and an optional `ListenerConfig` (ibkr_bridge WS with bearer token auth).
- **Multi-account support** via `_2` suffixed env vars (e.g. `IBKR_FLEX_QUERY_ID_2`). Each suffix produces an additional `PollerConfig` within the same relay — no separate container needed. Triggered via `make poll RELAY=ibkr IDX=2` or `POST /relays/ibkr/poll/2`.
- **Relay-specific overrides** — env vars like `IBKR_NOTIFIERS`, `IBKR_TARGET_WEBHOOK_URL` override the generic equivalents for the IBKR relay only, allowing different webhook destinations per broker.
- **Listener connect callback** — provides a closure that adds bearer token auth headers and tracks `last_seq` for event resumption across reconnects.
- **`flex_fetch.py` is a pure library** — it exposes `fetch_flex_report()` and `RedactTokenFilter` but contains no CLI code. It is imported by `__init__.py` (relay runtime) and `flex_dump.py` (CLI). Never add `if __name__ == "__main__"` blocks or `argparse` back into `flex_fetch.py` — doing so causes a `sys.modules` conflict because `__init__.py` imports it at package load time.
- **`flex_dump.py` is the CLI entrypoint** — invoked via `python -m relays.ibkr.flex_dump --token TOKEN --query-id ID [--dump PATH]`. It receives credentials as explicit CLI args (sourced from `.env.relays` by the Makefile) rather than reading env vars directly, keeping env-var ownership in `__init__.py`'s getters.
- **`RedactTokenFilter` is public** (no underscore) — it is exported from `flex_fetch.py` and used by both `__init__.py` (relay runtime logging) and `flex_dump.py` (CLI logging). Private (`_`-prefixed) names are only for identifiers with no external consumers.
- **Option mapping** — for `assetCategory == "OPT"` fills, `Fill.symbol = contract.localSymbol.replace(" ", "")` (OCC ticker with spaces stripped, e.g. `"AVGO260620C00200000"`) and `Fill.option.rootSymbol = contract.symbol` (underlying, e.g. `"AVGO"`). IBKR pads the underlying to 6 characters with spaces in the raw OCC ticker — always strip them so `Fill.symbol` is URL-friendly. The `strike`, `expiryDate` (via `flex_date_to_iso()`), and `type` (`"call"`/`"put"` from the `putCall` attribute) are required — rows with missing or invalid option metadata are skipped with a parse error.
- **Fixture management** — `fixtures/sanitize.py` replaces real account/order/execution IDs in a raw Flex dump with synthetic values, then trims the fixture to at most 6 distinct orders (`max_orders` / `_MAX_ORDERS = 6`) while keeping all executions for the retained orders. Run `make ibkr-flex-refresh [S=_2]` to fetch a live response, auto-detect the report type (Activity Flex vs Trade Confirmation), sanitize it, and write to the appropriate fixture file. Raw dumps (before sanitization) must never be committed — they contain real account IDs. The two committed fixtures (`activity_flex_sample.xml`, `trade_confirm_sample.xml`) contain only synthetic IDs and are safe to commit.

### Kraken adapter

- **`build_relay(notifiers)`** constructs a `BrokerRelay` with a Kraken REST poller (TradesHistory endpoint) and an optional WS v2 listener (executions channel).
- **Single shared `KrakenClient`** — `build_relay()` calls `_resolve_client()` once and passes the resulting client to both `_build_poller_configs()` and `_build_listener_config()`. This is required for nonce correctness: Kraken tracks the highest nonce ever seen per API key and rejects any request with a lower nonce (`EAPI:Invalid nonce`). The `KrakenClient` holds a `threading.Lock` and a `_last_nonce` floor so nonces are strictly monotonic even when the poller and listener fire concurrently. Never create separate `KrakenClient` instances for the poller and listener — they would race on nonce ordering.
- **Poller** — `KrakenClient.get_trades_history()` returns JSON; the parse callback maps each trade via `_parse_rest_trade()` into a `Fill` with `source="rest_poll"`.
- **Listener** — the `connect` callback obtains a short-lived WS token via REST (`GetWebSocketsToken`), opens a websocket to `wss://ws-auth.kraken.com/v2`, sends a subscription message for the `executions` channel, and returns the ready websocket. The `on_message` callback uses `ws_parser.parse_executions()` to extract multiple fills per message with `source="ws_execution"`.
- **All asset classes are `"crypto"`** — Kraken is a crypto-only exchange.

### Fee normalisation convention (apply to all relay adapters)

When mapping a broker fill to a `Fill` model, use this priority order for the `fee` field:

1. **Prefer a pre-converted equivalent field** if the broker provides one (e.g. Kraken's `fee_usd_equiv`). It is always meaningful regardless of how many fee currencies are involved.
2. **Single-asset fallback** — if the broker provides a `fees` array, only aggregate entries when every entry shares the same `asset`. Summing across different assets (e.g. USD + BTC) produces a number in no real currency; return `0.0` instead.
3. **`abs()` per entry, not on the total** — fee quantities may be signed. Apply `abs(qty)` to each entry before summing, not `abs(sum(...))` at the end. `abs(-5 + 3) = 2` understates the true fee; `abs(-5) + abs(3) = 8` is correct.
4. Return `0.0` when no fee information is available.

See `services/relays/kraken/ws_parser.py` (`_extract_fee`) for the reference implementation.

### Timestamp normalisation convention (apply to all relay adapters)

Every `Fill.timestamp` reaching the engine **must** be in the canonical form `YYYY-MM-DDTHH:MM:SS` — always UTC, no `Z` suffix, no `+00:00`, no fractional seconds. Lexicographic order equals chronological order (this is relied on by the poll-watermark comparison in `poller_engine.py`).

The normalisation pipeline has two layers with a strict split of responsibilities:

1. **Relay-owned** — broker-specific format → ISO-8601. Lives in `services/relays/<name>/timestamps.py`. One small function per native format, each using `strptime` to validate strictly and return a naive ISO-8601 string.
2. **Shared** — ISO-8601 → canonical UTC. Lives in `services/shared/time_format.py::normalize_timestamp(iso, *, assume_tz=None)`. Applies `assume_tz` to naive inputs, converts tz-aware inputs to UTC, strips fractional seconds. It **only** accepts ISO-8601 — never teach it about broker formats.

Call sites chain the two:

```python
ts = normalize_timestamp(flex_to_iso(raw), assume_tz=tz)    # IBKR Flex
ts = normalize_timestamp(bridge_to_iso(raw), assume_tz=tz)  # IBKR bridge
ts = normalize_timestamp(raw)                               # Kraken (already ISO)
```

**Why the split matters.** Without it, every new relay would append another regex or `strptime` branch to `shared/time_format.py`, which would become a junk drawer of broker quirks. Keeping format knowledge colocated with the relay package means:

- Each broker's format is documented and tested next to the code that produces it.
- `shared/time_format.py` stays tiny and broker-agnostic.
- `datetime.fromisoformat` in Python 3.12+ is _very_ lenient (it accepts IBKR-style `YYYYMMDD-HH:MM:SS` and `YYYYMMDD;HHMMSS` directly). The relay-level helper exists specifically to **reject typos and wrong separators** that `fromisoformat` would silently misinterpret — it's a validation gate, not a parsing fallback.

**Timezone handling.** Brokers that emit naive timestamps (IBKR Flex, IBKR bridge) need a `{RELAY}_ACCOUNT_TIMEZONE` env var (e.g. `IBKR_ACCOUNT_TIMEZONE=America/New_York`). Read it via a getter that calls `shared.parse_timezone(name)` and converts `ValueError` to `SystemExit` at boot. The resulting `ZoneInfo` is threaded into parse callbacks via `build_relay()` (closure-capture, not re-read per fill). Brokers that emit tz-aware timestamps (Kraken with `Z`) don't need an env var — `normalize_timestamp` ignores `assume_tz` when the input is tz-aware.

See `services/relays/ibkr/timestamps.py` and `services/shared/time_format.py` for the reference implementation.

## Notifier Package

The `services/relay_core/notifier/` package provides a pluggable notification backend system used by all relays.

```
services/relay_core/notifier/
  __init__.py              # Registry, load_notifiers(), validate_notifier_env(), notify()
  base.py                  # BaseNotifier ABC (name, required_env_vars, send, default env validation)
  webhook.py               # WebhookNotifier: HMAC-SHA256 signed HTTP POST
  test_notifier.py         # Tests for registry and loader
  test_webhook.py          # Tests for webhook backend
```

- **`NOTIFIERS` env var** controls which backends are active (comma-separated, e.g. `NOTIFIERS=webhook`). Empty = no notifications (dry-run).
- **Prefix support** — relay adapters can pass a prefix (e.g. `IBKR_`) to read from `IBKR_TARGET_WEBHOOK_URL`, `IBKR_WEBHOOK_SECRET`, etc. This enables per-relay webhook destinations.
- **Suffix support** — `_2` suffixed env vars enable separate webhook destinations for multi-account pollers within a single relay.
- **Validation belongs in each notifier's `__init__`, not the coordinator.** The coordinator (`__init__.py`) is a registry + dispatcher — it must not contain backend-specific validation logic. Each `BaseNotifier` subclass validates its own env vars in its constructor and raises `SystemExit` on misconfiguration.
- **`validate_notifier_env()`** is called by `cli/__init__.py` during pre-deploy checks. It instantiates each configured backend (triggering constructor validation) and converts `SystemExit` to a `die()` call for CLI-friendly output.
- **Adding a new backend** — create `services/relay_core/notifier/<name>.py` with a class extending `BaseNotifier`, add it to `REGISTRY` in `__init__.py`. The constructor must validate all required env vars.
- **The relay engines resolve notifiers from the relay context** — notifiers are loaded once at startup per relay, stored on `BrokerRelay`, and accessed via `get_relay(name).notifiers`. The engines have no direct knowledge of webhook delivery mechanics.
- **Debug webhook URL resolution** — `WebhookNotifier.__init__` calls `_resolve_webhook_url()`. If `DEBUG_WEBHOOK_PATH` is set, the URL is overridden to `http://debug:9000/debug/webhook/{path}` (container-to-container DNS). Otherwise, it reads `TARGET_WEBHOOK_URL`. No env var mutation occurs — the resolved URL is stored in `self._url`.

## Debug Webhook Service

The `services/debug/` service is a **standalone aiohttp container** that captures webhook payloads for inspection during development and debugging.

```
services/debug/
  debug_app.py             # aiohttp app: POST/GET/DELETE /debug/webhook/{path} + GET /health
  Dockerfile               # python:3.11-slim, runs debug_app.py
  requirements.txt         # aiohttp only
  test_debug.py            # Unit tests
```

- **`DEBUG_WEBHOOK_PATH`** env var controls the accepted path segment. Requests to any other path return 404. When unset, the container is not running (`DEBUG_REPLICAS=0`).
- **In-memory inbox** — `_inbox: list[PayloadEntry]` stores received payloads (payload + headers + timestamp). Capped at `MAX_DEBUG_WEBHOOK_PAYLOADS` (default 100, hard max 150) with FIFO eviction.
- **Endpoints**: `POST /debug/webhook/{path}` captures a payload, `GET` returns all stored payloads, `DELETE` clears the inbox. `GET /health` returns status.
- **Logging**: Summary at INFO level, full payload+headers at DEBUG level. Set `DEBUG_LOG_LEVEL=DEBUG` in `.env` and `docker logs -f debug` to tail payloads. Aggressive log rotation (`max-size: 10k`, `max-file: 1`) keeps disk usage minimal.
- **No auth** — the debug path in the URL acts as a shared secret. The service is not exposed to the internet unless Caddy routes to it via `debug.caddy`.
- **Port 9000** is hardcoded (`HTTP_PORT = 9000`). In production, Caddy reverse-proxies to `debug:9000` — no host port mapping needed. Local dev uses `15003:9000` (`docker-compose.local.yml`), E2E uses `15012:9000` (`docker-compose.test.yml`).
- **Module name**: `debug_app.py` (not `main.py`) to avoid `sys.modules` collisions when both are on `sys.path`.

## Dedup Package

The `services/relay_core/dedup/` package provides SQLite dedup logic used by the poller and listener engines.

```
services/relay_core/dedup/
  __init__.py              # init_db(), is_processed(), mark_processed(), get_processed_ids(), mark_processed_batch(), prune()
  test_dedup.py            # Tests for dedup module
```

- **`init_db(db_path)`** creates the `processed_fills` table and returns a `sqlite3.Connection`.
- **`get_processed_ids(conn, exec_ids)`** — batch check (used by poller engine).
- **`mark_processed_batch(conn, exec_ids)`** — batch mark (used by poller engine).
- **`prune(conn, days=30)`** — delete old entries.
- **Dedup key priority** — `ibExecId → transactionId → tradeID`, resolved in `services/relays/ibkr/flex_parser.py` at parse time by setting `Fill.execId`. `services/shared/utilities.py::_dedup_id()` simply returns the already-resolved `fill.execId`.
- The poller engine has a separate metadata DB at `META_DB_PATH` (default `/data/meta/<relay>.db`) on a `relay-meta` volume for the timestamp watermark.

## Models (Three Locations)

This project has **three model locations** — each owns a distinct contract layer:

| File                                     | Domain                      | Contains                                                                     |
| ---------------------------------------- | --------------------------- | ---------------------------------------------------------------------------- |
| `services/shared/models.py`              | CommonFill primitives       | `Fill`, `Trade`, `OptionContract`, `BuySell`, `AssetClass`, `OrderType`, `Source`, `RelayName` |
| `services/relay_core/notifier/models.py` | Notifier payload (outbound) | `WebhookPayloadTrades`, `WebhookPayload`                                     |
| `services/relay_core/relay_models.py`    | Relay API (outbound)        | Re-exports notifier payload + `RunPollResponse`, `HealthResponse`            |

- **`services/shared/models.py`** defines the CommonFill primitives. The `__init__.py` barrel re-exports them so `from shared import Fill` keeps working.
- **`services/relay_core/notifier/models.py`** is the authoritative home for outbound webhook payload contracts. When you want to know "what does the notifier send?", this is where to look. Add new payload variants here as new event types are introduced.
- **`services/shared/utilities.py`** contains internal helpers (`aggregate_fills`, `normalize_order_type`, `normalize_asset_class`, `_dedup_id`). These are not exported to consumer packages.
- **Model shims only re-export models and types** (Pydantic models, enums, type aliases). Utility functions (`aggregate_fills`, `normalize_order_type`, `_dedup_id`) must be imported directly from the owning module: `from shared import aggregate_fills`. Never re-export functions through model shims.
- `relay_models.py` re-exports the notifier payload contracts and defines relay-specific API types. Its exported models (`WebhookPayloadTrades`, `RunPollResponse`, `HealthResponse`) are listed in `schema_gen.py:SCHEMA_MODELS` under the key `"relay_core.relay_models"`.
- `shared/models.py` exports the CommonFill primitives (`Trade`, `Fill`). These are listed in `schema_gen.py:SCHEMA_MODELS` under the key `"shared"`.
- All external-contract models use `ConfigDict(extra="forbid")` for strict validation.

## TypeScript Types

### Namespace Convention

All relay projects export TypeScript types using a two-tier namespace pattern:

- **`types/typescript/shared/`** → exported as `BrokerRelay`. Contains the CommonFill primitives (`Fill`, `Trade`, `BuySell`) generated via `schema_gen.py` from `services/shared/models.py`.
- **`types/typescript/relay_api/`** → exported as `RelayApi`. Contains the notifier payload contracts (`WebhookPayloadTrades`, `WebhookPayload`) and relay API types (`RunPollResponse`, `HealthResponse`) generated via `schema_gen.py` from `services/relay_core/relay_models.py`.

The barrel `types/typescript/index.d.ts` ties them together:

```ts
import * as BrokerRelay from "./shared";
import * as RelayApi from "./relay_api";
export { BrokerRelay, RelayApi };
```

### Types Package

- Types are published as `@tradegist/relayport-types` (npm package in `types/typescript/`, not yet published).
- **Two namespaces**: `BrokerRelay` (CommonFill primitives) and `RelayApi` (notifier payload contracts + relay API types).
- **`make types`** regenerates both from Pydantic models (depends on `typecheck`):
  - `services/shared/models.py` → `types/typescript/shared/types.d.ts`
  - `services/relay_core/relay_models.py` → `types/typescript/relay_api/types.d.ts`
  - Also generates Python type packages via `gen_python_types.py`.
- **Structure:**
  ```
  types/typescript/
    index.d.ts                 # Barrel: exports BrokerRelay, RelayApi namespaces
    package.json               # @tradegist/relayport-types
    shared/
      index.d.ts               # Re-exports: BuySell, Fill, Trade
      types.d.ts               # Generated from services/shared/models.py
      types.schema.json         # Intermediate JSON Schema
    relay_api/
      index.d.ts               # Re-exports: WebhookPayloadTrades, WebhookPayload, RunPollResponse, HealthResponse
      types.d.ts               # Generated from services/relay_core/relay_models.py (via relay_core.relay_models key)
      types.schema.json         # Intermediate JSON Schema
  ```
- **Usage:** `import { BrokerRelay, RelayApi } from "@tradegist/relayport-types"`
- `schema_gen.py` owns the `SCHEMA_MODELS` dict (keyed by importable module path, e.g. `"shared"`, `"relay_core.relay_models"`). **To export a new model to TypeScript, add it to the relevant entry in `schema_gen.py:SCHEMA_MODELS` and update the corresponding `types/typescript/*/index.d.ts` re-exports.** The Python types package is auto-generated by `gen_python_types.py`.

## Python Types Package

- Types are available as `relayport-types` (PyPI package in `types/python/`, not yet published).
- **Standalone Pydantic models** — no dependency on the relay service.
- Mirrors the `relay_core` source structure so the package layout reflects the code design.
- **Structure:**
  ```
  types/python/
    pyproject.toml              # relayport-types, deps: pydantic
    relayport_types/
      __init__.py               # Re-exports all public types
      shared.py                 # CommonFill primitives (generated from services/shared/models.py)
      relay_api.py              # Relay API types (generated from services/relay_core/relay_models.py)
      notifier/
        __init__.py
        models.py               # Payload contracts (generated from relay_core/notifier/models.py)
  ```
- **Usage:**
  ```python
  from relayport_types import Fill, Trade, BuySell              # CommonFill primitives
  from relayport_types import WebhookPayload, WebhookPayloadTrades  # notifier contracts
  from relayport_types.notifier.models import WebhookPayloadTrades  # direct path
  ```
- **Auto-generated** by `gen_python_types.py` — each source file is copied verbatim with one import-depth rewrite. Run `make types` to regenerate. Do not edit generated files manually.
- **Covered by `make lint` and `make typecheck`** — `types/python/relayport_types/` is included in both targets. Generated code must pass ruff and mypy like any other Python module.

## Code Style

- Python: `logging` module, f-strings, `aiohttp` for async HTTP in relay service, `httpx` for sync HTTP client in poller engine.
- CLI scripts: Python (`cli/` package), invoked via `python3 -m cli <command>` or `make`. Uses only stdlib (`subprocess`, `urllib.request`, `json`, `os`). No third-party dependencies. Uses lazy dispatch (`importlib.import_module`) — each command only imports its own module.
- Terraform: all secrets marked `sensitive = true` in `variables.tf`.

## Build & Deploy

All commands available via `make` or `python3 -m cli <command>`:

```bash
make deploy    # Standalone: Terraform | Shared: rsync + compose (reads .env.droplet)
make sync      # Push .env + .env.relays to droplet + restart services
make sync LOCAL_FILES=1  # rsync files + rebuild + restart (full code deploy)
make destroy   # Terraform destroy
make pause     # Snapshot + delete droplet (save costs)
make resume    # Restore from snapshot
make poll      # Trigger immediate poll (RELAY=ibkr, IDX=1)
make watermark-reset    # Reset timestamp watermark to now [RELAY=ibkr or empty for all] [ENV=local]
make ibkr-flex-dump     # Dump live IBKR Flex XML to fixtures/raw.xml (F=path to override, S=_2 for second account)
make ibkr-flex-refresh  # Fetch live Flex, auto-detect type, sanitize, write fixture (S=_2 for second account)
make e2e       # Run E2E tests (starts/stops stack)
make lint      # Run ruff linter (FIX=1 to auto-fix)
```

Direct CLI (no Make required, works on Windows):

```bash
python3 -m cli deploy
python3 -m cli sync --local-files
python3 -m cli poll ibkr 1
python3 -m cli watermark-reset            # all relays
python3 -m cli watermark-reset ibkr       # single relay
```

## Deployment Model (MANDATORY)

- **`make sync LOCAL_FILES=1` uses rsync** to transfer files from the local working tree to `/opt/relayport/` on the droplet. It does NOT use git on the droplet — no git clone, no deploy keys, no GitHub access needed from the server.
- **Guards:** Must be on `main` branch with a clean working tree (no uncommitted changes). This ensures rsync deploys a known committed state.
- **`--delete` flag:** rsync removes files on the droplet that no longer exist locally. This correctly handles renames and deletions but is dangerous for server-generated files.
- **Invariant: the project directory (`/opt/relayport/`) contains only source files.** No service, script, or container may write files into the project directory. All runtime-generated data (databases, caches, logs, certificates) MUST use Docker named volumes (e.g. `dedup-data:/data/dedup`, `relay-meta:/data/meta`, `caddy-data:/data`). Docker volumes live under `/var/lib/docker/volumes/`, completely outside the project directory, and are safe from rsync `--delete`.
- **When adding new runtime data** (a new database, cache file, upload directory, etc.): create a Docker named volume in `docker-compose.yml` and mount it into the container. Never write to a path inside `/opt/relayport/`.
- **`.deployed-sha`** is the only server-side file inside the project directory. It is written by `cli/sync.py` after each `--local-files` sync and is excluded from rsync `--delete`. It records the deployed commit SHA for traceability.
- **rsync exclusions** (files never overwritten or deleted on the droplet):
  - `.git/` — not present on droplet (no git repo)
  - `.env` — pushed separately via scp (contains secrets)
  - `.env.relays` — pushed separately via scp (contains relay secrets)
  - `.env.droplet` — never pushed to droplet (CLI-only)
  - `.env.test` — local-only test config
  - `.deployed-sha` — server-side deployment marker
  - Everything in `.gitignore` — via `--filter ':- .gitignore'`

## File Structure

```
env_examples/              # Env var templates (make setup copies to .<name>)
  env                      # App config (.env)
  env.droplet              # CLI-only deployment config (.env.droplet)
  env.relays               # Relay-prefixed vars (.env.relays)
  env.test                 # E2E test config (.env.test)
docker-compose.yml         # All services (caddy, relays, debug)
docker-compose.shared.yml  # Shared-mode overlay (disables Caddy, uses SHARED_NETWORK)
docker-compose.local.yml   # Local dev override (direct port access, no TLS)
docker-compose.test.yml    # Test stack override (env_file: !override with .env.test)
cli/                       # Python CLI (operator scripts, stdlib only)
  __init__.py              # Shared helpers (env loading, SSH, DO API, validation)
  __main__.py              # Entry point (lazy dispatch via importlib)
  core/
    __init__.py            # CoreConfig, load_env() — loads .env.droplet + .env + .env.relays
    deploy.py              # Standalone (Terraform) or shared (rsync + compose)
    destroy.py             # Terraform destroy
    pause.py               # Snapshot + delete droplet
    resume.py              # Restore from snapshot
    sync.py                # Push .env + .env.relays + restart services
  poll.py                  # Trigger immediate poll (relay + index)
  test_webhook.py          # Send test webhook payload
services/                  # Business-logic services
  relay_core/              # Main container: registry + engines + HTTP API
    __init__.py            # BrokerRelay dataclass, re-exports engine types
    main.py                # Entrypoint (loads relays, starts pollers + listeners + API)
    registry.py            # Relay registry (RELAYS env var → adapter loading)
    poller_engine.py       # Generic poller (dedup, fetch, parse, notify, mark)
    listener_engine.py     # Generic WS listener (connect, dedup, notify, reconnect)
    relay_models.py        # Re-export shim (shared models + RunPollResponse, HealthResponse)
    dedup/                 # SQLite dedup library
      __init__.py          # init_db(), is_processed(), mark_processed(), prune()
    notifier/              # Pluggable notification backends
      __init__.py          # Registry, load_notifiers(), validate_notifier_env(), notify()
      base.py              # BaseNotifier ABC
      webhook.py           # WebhookNotifier: HMAC-SHA256 signed HTTP POST
    routes/                # HTTP API
      __init__.py          # create_app(), start_api_server(), handle_health, handle_poll
      middlewares.py       # Auth middleware (Bearer token, AUTH_PREFIX=/relays)
    tests/e2e/             # E2E tests
      conftest.py          # httpx fixtures + two-tier preflight
      test_smoke.py        # Health + auth smoke tests
    Dockerfile
    requirements.txt
  relays/                  # Broker adapters (one package per broker)
    ibkr/                  # IBKR adapter
      __init__.py          # build_relay(), env getters, map_fill()
      bridge_models.py     # Mirrored WsEnvelope types from ibkr_bridge
      flex_fetch.py        # Flex Web Service two-step fetch (pure library)
      flex_dump.py         # CLI entrypoint: fetch + write to disk
      flex_parser.py       # Flex XML parser (Activity + Trade Confirmation)
      fixtures/
        sanitize.py        # Sanitize raw Flex dump → committable fixture
        activity_flex_sample.xml   # Sanitized Activity Flex fixture
        trade_confirm_sample.xml   # Sanitized Trade Confirmation fixture
    kraken/                # Kraken crypto exchange adapter
      __init__.py          # build_relay(), env getters, REST poller + WS listener
      rest_client.py       # KrakenClient: HMAC-SHA512 auth, trades history, WS token
      ws_parser.py         # WS v2 executions channel parser
      kraken_types.py      # TypedDicts for raw Kraken API shapes
  shared/                  # Shared models and utilities (library, no container)
    __init__.py            # Barrel: re-exports models + utilities
    models.py              # Pydantic models (Fill, Trade, WebhookPayload, BuySell, RelayName)
    utilities.py           # Internal helpers (aggregate_fills, normalize_*, _dedup_id)
  debug/                   # Debug webhook inbox service
    debug_app.py           # aiohttp app: POST/GET/DELETE /debug/webhook/{path}
    Dockerfile
    requirements.txt
infra/                     # Infrastructure backbone (no business logic)
  caddy/Caddyfile          # Reverse proxy config (uses env vars for domains)
  caddy/sites/             # Route snippets imported inside {$SITE_DOMAIN}
    ibkr.caddy             # /relays/* routes → relays:8000
    debug.caddy            # /debug/webhook/* → debug:9000
types/                     # Type packages (TypeScript + Python)
  typescript/              # @tradegist/relayport-types (BrokerRelay + RelayApi namespaces)
  python/                  # relayport-types PyPI package
schema_gen.py              # JSON Schema generator (Pydantic → TS types)
gen_python_types.py        # Python types generator (mirrors relay_core structure → types/python/relayport_types/)
terraform/                 # Infrastructure as code (DigitalOcean)
```
