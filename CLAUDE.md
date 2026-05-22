# RelayPort — Project Guidelines

RelayPort is a **relay between broker accounts**: a common interface that bridges multiple brokers (currently IBKR and Kraken) to outbound notification layers (currently webhooks). Current flow: Broker → User (trade fill events). Future: User → Broker (order placement).

This file holds **cross-cutting rules** that apply everywhere. Per-directory rules live in nested `CLAUDE.md` files (loaded on demand when Claude touches files in that subtree). Architectural prose lives in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Where to find more guidance

| When you are working in… | Read… |
|---|---|
| `cli/` | [cli/CLAUDE.md](cli/CLAUDE.md) — deploy modes, env files, rsync invariant, Makefile rules |
| `services/` (anywhere) | [services/CLAUDE.md](services/CLAUDE.md) — test conventions, three-model layout |
| `services/relay_core/` | [services/relay_core/CLAUDE.md](services/relay_core/CLAUDE.md) — registry, engines, notifier, dedup, auth |
| `services/relays/` | [services/relays/CLAUDE.md](services/relays/CLAUDE.md) — fee + timestamp normalisation |
| `services/relays/ibkr/` | [services/relays/ibkr/CLAUDE.md](services/relays/ibkr/CLAUDE.md) |
| `services/relays/kraken/` | [services/relays/kraken/CLAUDE.md](services/relays/kraken/CLAUDE.md) |
| `services/market_data/` | [services/market_data/CLAUDE.md](services/market_data/CLAUDE.md) — Yahoo client, error hierarchy |
| `services/debug/` | [services/debug/CLAUDE.md](services/debug/CLAUDE.md) |
| `services/shared/` | [services/shared/CLAUDE.md](services/shared/CLAUDE.md) |
| `infra/` | [infra/CLAUDE.md](infra/CLAUDE.md) — Caddy snippets, `route_prefixes` |
| `types/` | [types/CLAUDE.md](types/CLAUDE.md) — TS + Python types regeneration |

**Playbooks** (rare procedures) live as skills in [.claude/skills/](.claude/skills/):
- `add-relay-adapter` — full 11-step procedure for adding a new broker
- `refresh-flex-fixtures` — sanitize and commit Flex XML fixtures
- `add-caddy-route` — add a new routed service (snippet + `route_prefixes` + token + alias)
- `export-new-model-to-types` — register a model in `SCHEMA_MODELS` and regenerate

## Code Quality (MANDATORY)

- **Always apply best practices by default.** Use idiomatic Python naming, file organization, and patterns. When a clearly better approach exists (naming, structure, error handling), use it directly and explain why — don't ask permission.
- **NEVER use deprecated APIs.** Examples: `asyncio.get_event_loop()` → `asyncio.get_running_loop()`; `datetime.utcnow()` → `datetime.now(UTC)`; Pydantic v1 `parse_obj` / `dict()` → v2 `model_validate` / `model_dump`. Scan docs for "deprecated" before relying on anything new. A deprecation warning in CR is a regression — fix the call, don't suppress.
- **No unused imports.** After any edit, verify every `import` is used. Remove what isn't.
- **No `__all__`.** All imports are explicit (`from module import X`); star-imports are never used.
- **No `assert` for runtime guards.** `assert` is stripped under `python -O`. Use `if … raise RuntimeError(...)` (or `die()`) for invariants that must hold at runtime.
- **Run `make lint` after every code change.** Ruff enforces unused imports (F401), import ordering (I001), unused variables, bugbear pitfalls, and modern idioms. `make lint FIX=1` auto-fixes safe issues.
- **Centralise env var reads into typed getter functions.** Each env var is read in exactly one place — a getter in the module that owns it (e.g. `_get_flex_token()` in `relays/ibkr/__init__.py`). Getters apply `.strip()` and type conversion. Never call `os.environ.get()` inline outside a getter.
- **Getters must validate and fail fast.** Every getter must validate and `raise SystemExit("<descriptive message>")` on bad input — never propagate silently. Wrap `int()` / `float()` in `try/except ValueError`. Check emptiness on required strings. Callers should never have to validate a getter's return value.
- **Prefer pure functions over side-effect functions.** Never write `apply_*()` / `set_*()` that silently mutates `os.environ`, globals, or module-level caches. Compute and return; let the caller decide. If a side-effect is truly unavoidable, add an inline comment at every call site: `# Mutates X to enable Y`.
- **Never bulk-set `os.environ` with empty-string fallbacks.** `os.environ[key] = env(name, "")` silently overrides downstream defaults (e.g. Terraform `variable` defaults, library config) with empty strings, breaking `tonumber()`, validation blocks, and non-string parsing. Only export when source value is present and non-empty; `os.environ.pop(key, None)` otherwise.
- **Verify Markdown table integrity after every edit.** Insert/rewrite a `|`-delimited row → count column dividers on the changed row AND the header/separator rows. All must match. Two known failure modes: (1) a bare `|` inside a cell (`HH:MM AM|PM`) splits the row — escape as `\|` or rewrite (`HH:MM AM/PM`); (2) an extra `| ----- |` segment in a separator row. Sanity check: `awk -F'\|' 'NR>=START && NR<=END { print NR": "NF" cells" }' README.md` — every row must report the same `NF`.
- **Update README.md when changing public interfaces.** CLI commands, Makefile targets, API endpoints, env vars — reflect the change.

## Security Rules (MANDATORY)

- **No hardcoded credentials.** Passwords, API tokens, secrets, keys must come from env vars (`.env`, `TF_VAR_*`). Never write real values in source files.
- **No hardcoded IPs.** Use `DROPLET_IP` from `.env.droplet`. In docs use `1.2.3.4` as placeholder.
- **No hardcoded domains.** Use `example.com` variants in docs and code (`trade.example.com`); actual domain loaded at runtime via `SITE_DOMAIN`.
- **No email addresses or personal info.** Never write real names, emails, or account IDs in committed files. Use `UXXXXXXX` for IBKR account examples.
- **No developer-machine paths.** Never write absolute paths like `/Users/john/…` or `C:\Users\john\…` in committed files. Reference sibling projects by name only.
- **No logging of secrets or sensitive operational data.** Never `log.info()` tokens, passwords, API keys, account IDs, account aliases, IPs, or domains. Log actions and outcomes, not credential values. Never log full model dumps at `info` — use `log.debug` with field exclusion: `log.debug("Trade: %s", trade.model_dump_json(exclude={"accountId", "acctAlias"}))`. Prefer counts, symbols, statuses over full objects.
- **`.env`, `.env.droplet`, `.env.relays`, `*.tfvars`, `.env.test` are gitignored.** Never commit them. Use `env_examples/` templates with placeholder values.
- **Raw Flex XML dumps must never be committed.** Live Flex responses contain real account IDs, execution IDs, and order IDs. Always sanitize via `make ibkr-flex-refresh` (or `fixtures/sanitize.py`). Intermediate raw file `fixtures/raw.xml` is gitignored. Only sanitized fixtures (`activity_flex_sample.xml`, `trade_confirm_sample.xml`) are committed.
- **Terraform state is gitignored** — `terraform.tfstate` contains SSH keys and IPs.
- **Auth middleware must reject empty tokens.** `hmac.compare_digest("", "")` returns `True`, so an empty `API_TOKEN` / `MD_API_TOKEN` silently disables auth. Every auth middleware must `if not _TOKEN: return HTTP 500` **before** reaching `compare_digest`. `API_TOKEN` is in `required_env` for deploy/sync — CLI blocks deployment if missing.

## Type Safety (MANDATORY)

- **Python >= 3.11.** Uses `X | None` union syntax natively (no `from __future__ import annotations`). Docker images use `python:3.11-slim`.
- **Run `make typecheck`, `make test`, and `make lint` after every code change.** Non-negotiable before deploying. mypy + ruff + pytest must all pass. `make typecheck` also runs `tsc --noEmit` on `types/typescript/`.
- **Run E2E tests after modifying any E2E test OR infrastructure file** (`docker-compose*.yml`, `Dockerfile`, `Caddyfile`, anything under `infra/`). Workflow:
  1. `make e2e-up` — start the stack (idempotent).
  2. `make e2e-run` — run tests.
  3. Fix code → `make e2e-run` → repeat. Volume mounts keep code in sync — no rebuild.
  4. `make e2e-down` — tear down **only after all tests pass**, never between iterations.
- **Every Python file must be covered by `make typecheck`.** New service/package/standalone script → add to the mypy invocation in the Makefile. Files inside an existing whole-directory target (`cli/`, `services/relay_core/`) are covered automatically.
- **Register new modules in `pyproject.toml`.** When adding a new Python service/package under `services/` or `types/python/`: add to (1) `tool.pytest.ini_options.testpaths`, (2) `tool.ruff.src`, (3) `tool.ruff.lint.isort.known-first-party`, (4) Makefile `lint:` and `typecheck:` targets. Exception: `cli/` is already covered as a whole directory.
- After modifying any model in `services/shared/models.py`, `services/relay_core/notifier/models.py`, `services/relay_core/relay_models.py`, or `services/market_data/models/dividends.py`, run `make types` to regenerate TypeScript + Python type packages.
- **Always verify type safety by breaking it first.** After any refactor that touches types, deliberately introduce a type error, run `make typecheck`, confirm it **fails**. Then revert and confirm it passes. Never assume mypy catches something — prove it.
- **Avoid `dict[str, Any]` round-trips.** Never `model_dump()` → `dict` → `Model(**data)` — mypy can't type-check `**dict[str, Any]`. Use explicit kwargs or `model_copy(update=...)`.
- **Prefer strict `Literal` over bare `str` on Pydantic models.** Financial code demands precision — `str` silently accepts typos. Use existing `Literal` types (`BuySell`, `OrderType`, `AssetClass`) when the value set is known. Fall back to `str` only when the external source is genuinely unbounded — document why inline.
- **No `# type: ignore` without justification.** Fix the root cause: annotation, import, widening, or `cast()`. If truly unavoidable (untyped third-party lib): `# type: ignore[attr-defined] # ib_async.Foo has no stubs`. Bare `# type: ignore` is never acceptable.
- **Use `cast()` instead of `# type: ignore[arg-type]`.** When passing a mock or compatible object where mypy expects a concrete type, `cast(TargetType, mock)` — not `# type: ignore`. `cast()` preserves downstream type-checking; `# type: ignore` silently disables it.
- **Use `@overload` for sentinel-default patterns.** When a function accepts an optional default via a sentinel (`_UNSET = object()`), express the two signatures via `@overload` instead of `# type: ignore` on the return. Use `cast()` in the impl body.

## Pydantic Best Practices

- **`Field(default_factory=list)`** for mutable defaults — only when genuinely optional. Never bare `[]` or `{}`.
- **Do not add defaults to fields that are always populated.** A default (`= 0`, `= Field(default_factory=list)`) makes the field optional in the generated JSON Schema / TypeScript (`fillCount?: number`). Only use defaults for fields that are legitimately absent in some cases.
- **`ConfigDict(extra="forbid")`** on external-contract models (webhook payloads, API responses). Produces `additionalProperties: false` in JSON Schema, keeping generated TS strict.
- **Docstrings claiming "never raises"** must match the impl — wrap any call that can throw (e.g. `ET.fromstring()`) in try/except and return errors via the result tuple.

## Error Handling (MANDATORY)

- **Every error must produce a clear, actionable message.** Explain _what_ failed and _why_. Include context: operation, input identifier, upstream status. Never raise a generic "something went wrong".
- **API responses must never leak internal details.** Return structured error JSON with appropriate HTTP status and a human-readable `error` field. Never expose tracebacks, file paths, or internal class names to callers. Log full exception server-side at `error`/`exception`.
- **Isolate failures.** When dispatching to multiple backends/services, wrap each call in `try/except Exception`, log, continue. A single broken notifier must not crash the poll cycle.
- **Never silently swallow errors.** Every `except` must log (`log.exception(...)`) or re-raise. Bare `except: pass` is never acceptable.
- **`log.exception()` for unexpected errors** (auto-includes traceback at ERROR). Reserve `log.error()` for known/expected failures where a traceback is noise.
- **Distinguish recoverable from fatal.** Network timeouts → log, retry, skip. Missing config / corrupted state → fail fast with `raise SystemExit(msg)` or `die()`. Never limp along.
- **`SystemExit` must carry a descriptive message.** Never `raise SystemExit(1)` — callers that catch (e.g. `validate_notifier_env()`) lose all context. Always `raise SystemExit("Notifier 'webhook' requires env vars: WEBHOOK_SECRET")`.
- **Env var parsing must fail fast, not fall back silently.** Wrap `int()`/`float()` in `try/except ValueError: raise SystemExit(f"Invalid VAR={raw!r} — must be an integer")`. Fall back only on _missing_ vars (intentional default), never on _invalid_ values.
- **Validate at system boundaries, trust internally.** Validate at the entry point (API payloads, env vars, webhook data, Flex XML). Once validated, internal code does not re-validate — types and Pydantic carry the guarantees.
- **Never assume a default for financial enum fields.** `BuySell.BUY if x == "buy" else BuySell.SELL` treats any non-buy value (typos, nulls, garbage) as SELL. Always enumerate every valid value explicitly and raise on unknown. Applies to trade direction, order type, asset class, etc.
- **`fee` is always positive (amount paid).** Industry standard (FIX, Alpaca, Coinbase, Kraken). IBKR Flex reports commissions as negative numbers; parsers normalize via `abs()`. Never store or forward negative fees.
- **Never silently drop rows with missing identifiers.** When parsing Flex XML / REST JSON / WS messages, if a required identifier (e.g. `execId`) is missing after all fallback chains, report a parse error and skip the row explicitly. Don't let it fall through to a later guard (like a dedup check on empty string) where the drop is invisible.
- **HTTP handlers must catch and map exceptions.** Every aiohttp route handler must have a top-level `try/except` returning structured JSON 500. Unhandled exceptions produce ugly default responses and can leak internals.
- **Include context in error messages.** Bad: `"Failed to fetch Flex report"`. Good: `"Failed to fetch Flex report: query 12345 — HTTP 500 from IBKR"`.

## Reliability (MANDATORY)

- **Mark-after-notify, never before.** `mark_processed_batch()` runs ONLY AFTER `notify()` succeeds. A crash between mark and notify silently drops fills — recorded as processed, webhook never sent, next cycle skips it. Unrecoverable data loss.
- **Correct pattern:** run `notify()` and `mark_processed_batch()` sequentially in the same execution context (same thread or same `asyncio.to_thread` call). If `notify()` raises, the fill stays unprocessed and is retried.
- **Never separate mark from notify with an `await` boundary.** In async code, an `await` between mark and notify allows the process to crash between them. Keep atomic inside one synchronous block.
- **Replay mode is the exception.** `poll --replay N` intentionally skips dedup — resends the last N fills without marking. By design for debugging/recovery.
- **SQLite commits must be explicit.** After any `INSERT`/`UPDATE`, call `conn.commit()` immediately. A crash without explicit commit loses the write silently.

## Concurrency Safety (MANDATORY)

- **Assume concurrency by default.** The relay is async (aiohttp). Any handler can be interrupted at an `await`. Before merging any code touching shared state, ask: "Can two callers interleave here? What breaks?"
- **Never use TOCTOU patterns with locks.** Do NOT check `lock.locked()` then `async with lock:` — another coroutine can acquire in between. The lock acquisition must BE the check. Use `asyncio.wait_for(lock.acquire(), timeout=0)` with `try/finally: lock.release()` to fail-fast, or accept `async with lock:` will queue. Applies to all shared-state guards (locks, DB transactions, file locks, semaphores, balance checks).
- **Never share a `sqlite3.Connection` across threads.** `sqlite3.Connection` is not thread-safe. With `asyncio.to_thread()`, either pass the connection into a single synchronous function that does all DB work in one thread, or use an `asyncio.Lock` to serialise. Two concurrent `to_thread()` calls touching the same connection cause intermittent `OperationalError` and data corruption.
- **Poller engine `to_thread` pattern: create connections inside the worker thread.** Do NOT create `sqlite3.Connection` on the event-loop thread and pass into `asyncio.to_thread(poll_once, conn, ...)` — even with `check_same_thread=False`, this is cross-thread use. Instead, `poll_once()` creates thread-local connections internally (via `init_dedup_db()` / `init_meta_db()`), closes them in `finally`. Callers pass only non-DB arguments.
- **Financial operations require extra scrutiny.** Any code path that places orders, moves money, or modifies account state must be reviewed for: race conditions, double-execution, partial failure (crash between two steps), idempotency.
- **Use `asyncio.to_thread(func, *args)` to offload blocking calls**, never `loop.run_in_executor(None, func, *args)` unless you need a custom Executor.
- **Use `asyncio.get_running_loop()`, never `asyncio.get_event_loop()`.** `get_event_loop()` is deprecated since 3.10 outside a running loop.
- **Schedule background tasks via `asyncio.get_running_loop().create_task(coro)`, not `asyncio.ensure_future(coro)`.** `ensure_future` can attach to a stale loop. Retain the returned `Task` in a tracking set with `task.add_done_callback(set.discard)` to prevent GC mid-run.
- **Tasks stored in a dict keyed by external IDs must clean up on completion.** A `dict[ID, asyncio.Task]` leaks completed `Task` references forever if entries are never removed. For non-repeating ID streams (e.g. Kraken `orderId`s) the leak is unbounded. Wire `task.add_done_callback(cleanup)`, and have `cleanup(task)` pop the entry **only when the dict slot still points at the finishing task** (so a cancelled task whose slot has already been replaced does not evict its replacement):

  ```python
  task = asyncio.create_task(...)
  task.add_done_callback(functools.partial(self._cleanup_task, key))
  self._tasks[key] = task

  def _cleanup_task(self, key: str, task: asyncio.Task) -> None:
      if self._tasks.get(key) is task:
          self._tasks.pop(key, None)
  ```

- **SQLite schema migrations must be PRAGMA-gated AND race-safe.** `init_db` runs on every connection open — including the listener's per-flush connection, called many times per second. A bare `ALTER TABLE ... ADD COLUMN` parses the statement, raises `OperationalError("duplicate column name")`, briefly contends for the writer lock every time. Two layers: (1) gate DDL behind a cheap read-only `PRAGMA table_info(<table>)` check so steady-state is read-only; (2) wrap the `ALTER TABLE` in `contextlib.suppress(sqlite3.OperationalError)` to handle the concurrent-migration race on first deploy. Pattern:

  ```python
  cols = {row[1] for row in conn.execute("PRAGMA table_info(processed_fills)")}
  if "order_id" not in cols:
      with contextlib.suppress(sqlite3.OperationalError):
          conn.execute("ALTER TABLE processed_fills ADD COLUMN order_id TEXT")
  ```

## Dependency Management

- **Runtime deps** (e.g. `services/relay_core/requirements.txt`) use exact pins (`==`). Builds must be reproducible.
- **`requirements-dev.txt`** contains only dev-only tools (mypy, pytest, ruff). Runtime deps (`pydantic`, `httpx`, etc.) belong exclusively in the service's `requirements.txt`. Both files are installed together by CI / `make setup`. Never add a runtime dep to `requirements-dev.txt` — would create duplicate Dependabot PRs.
- **When adding a new dependency**, pin immediately. Runtime: exact pin in service `requirements.txt`. Dev: major-version constraint (`>=X,<X+1`) in `requirements-dev.txt`.
- **All services pinning the same dependency must use the same version.** Check existing pins: `grep -r 'aiohttp==' services/*/requirements.txt`.

## Code Style

- Python: `logging` module, f-strings, `aiohttp` for async HTTP in services, `httpx` for sync HTTP client in the poller engine.
- CLI: stdlib only (`subprocess`, `urllib.request`, `json`, `os`). No third-party deps. Lazy dispatch via `importlib.import_module`.
- Terraform: secrets marked `sensitive = true` in `variables.tf`.

## Sibling Project: ibkr_bridge

This project and its sibling `ibkr_bridge` share the same CLI deploy/destroy/sync infrastructure pattern. **Any change to `cli/core/deploy.py`, `cli/core/destroy.py`, or `cli/core/sync.py` here must be mirrored in the sibling project, and vice versa.** This includes Terraform state management, reserved IP handling, rsync exclusions, env file push logic, and compose startup commands. When you modify CLI core logic here, explicitly remind the user to apply the equivalent change to `ibkr_bridge`, and offer to do it in the same session.

---

**Maintenance note.** Each directory `CLAUDE.md` has a Copilot mirror at `.github/instructions/<name>.instructions.md` with an `applyTo:` glob. Root rules also mirror to `.github/copilot-instructions.md`. When editing any `CLAUDE.md`, update its mirror in the same commit. See [docs/INSTRUCTION_FILES.md](docs/INSTRUCTION_FILES.md) for the full layout and sync rules.
