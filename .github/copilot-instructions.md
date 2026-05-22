# RelayPort — Project Guidelines

RelayPort is a **relay between broker accounts**: a common interface that bridges multiple brokers (currently IBKR and Kraken) to outbound notification layers (currently webhooks). Current flow: Broker → User (trade fill events). Future: User → Broker (order placement).

These are the **always-on rules**. Path-scoped rules live in `.github/instructions/*.instructions.md` (loaded for matching files only). Architectural prose lives in [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md).

## Code Quality (MANDATORY)

- **Always apply best practices by default.** Use idiomatic Python naming, file organization, and patterns. When a clearly better approach exists, use it directly — don't ask permission.
- **NEVER use deprecated APIs.** Examples: `asyncio.get_event_loop()` → `asyncio.get_running_loop()`; `datetime.utcnow()` → `datetime.now(UTC)`; Pydantic v1 `parse_obj` / `dict()` → v2 `model_validate` / `model_dump`. Scan docs for "deprecated" before relying on anything new. A deprecation warning in CR is a regression — fix the call, don't suppress.
- **No unused imports.** After any edit, verify every `import` is used. Remove what isn't.
- **No `__all__`.** All imports are explicit (`from module import X`); star-imports are never used.
- **No `assert` for runtime guards.** `assert` is stripped under `python -O`. Use `if … raise RuntimeError(...)` (or `die()`) for invariants that must hold at runtime.
- **Run `make lint` after every code change.** Ruff enforces unused imports, import ordering, unused variables, bugbear pitfalls, modern idioms. `make lint FIX=1` auto-fixes safe issues.
- **Centralise env var reads into typed getter functions.** Each env var is read in exactly one place — a getter in the module that owns it. Getters apply `.strip()` and type conversion. Never call `os.environ.get()` inline outside a getter.
- **Getters must validate and fail fast.** Every getter must validate and `raise SystemExit("<descriptive message>")` on bad input. Wrap `int()`/`float()` in `try/except ValueError`. Check emptiness on required strings. Callers should never have to validate a getter's return value.
- **Prefer pure functions over side-effect functions.** Never write `apply_*()` / `set_*()` that silently mutates `os.environ`, globals, or module-level caches. Compute and return; let the caller decide. If unavoidable, add an inline comment at every call site: `# Mutates X to enable Y`.
- **Never bulk-set `os.environ` with empty-string fallbacks.** `os.environ[key] = env(name, "")` silently overrides downstream defaults with empty strings. Only export when source is present and non-empty; `os.environ.pop(key, None)` otherwise.
- **Verify Markdown table integrity after every edit.** Count column dividers on changed row(s) AND header/separator rows — all must match. Known failure modes: (1) bare `|` inside a cell splits the row — escape as `\|` or rewrite; (2) extra `| ----- |` in separator. Sanity check: `awk -F'\|' 'NR>=START && NR<=END { print NR": "NF" cells" }' file.md`.
- **Update README.md when changing public interfaces.** CLI commands, Makefile targets, API endpoints, env vars.

## Security Rules (MANDATORY)

- **No hardcoded credentials.** Use env vars (`.env`, `TF_VAR_*`). Never real values in source.
- **No hardcoded IPs.** Use `DROPLET_IP` from `.env.droplet`. In docs use `1.2.3.4` as placeholder.
- **No hardcoded domains.** Use `example.com` variants in docs and code; runtime via `SITE_DOMAIN`.
- **No email addresses or personal info.** Use `UXXXXXXX` for IBKR account examples.
- **No developer-machine paths.** Never `/Users/john/…` or `C:\Users\john\…` in committed files. Reference sibling projects by name only.
- **No logging of secrets or sensitive operational data.** Never `log.info()` tokens, passwords, keys, account IDs, account aliases, IPs, or domains. Never log full model dumps at `info` — use `log.debug` with field exclusion: `log.debug("Trade: %s", trade.model_dump_json(exclude={"accountId", "acctAlias"}))`. Prefer counts, symbols, statuses.
- **`.env`, `.env.droplet`, `.env.relays`, `*.tfvars`, `.env.test` are gitignored.** Never commit them.
- **Raw Flex XML dumps must never be committed.** Sanitize via `make ibkr-flex-refresh` (or `fixtures/sanitize.py`). Only `activity_flex_sample.xml` and `trade_confirm_sample.xml` are committed.
- **Terraform state is gitignored** — `terraform.tfstate` contains SSH keys and IPs.
- **Auth middleware must reject empty tokens.** `hmac.compare_digest("", "")` returns `True`, so empty `API_TOKEN` / `MD_API_TOKEN` silently disables auth. Check `if not _TOKEN: return HTTP 500` **before** `compare_digest`. `API_TOKEN` is in `required_env` for deploy/sync.

## Type Safety (MANDATORY)

- **Python >= 3.11.** Uses `X | None` natively (no `from __future__ import annotations`). Docker uses `python:3.11-slim`.
- **Run `make typecheck`, `make test`, and `make lint` after every code change.** Non-negotiable before deploying. mypy + ruff + pytest must all pass.
- **Run E2E tests after modifying any E2E test OR infrastructure file** (`docker-compose*.yml`, `Dockerfile`, `Caddyfile`, anything under `infra/`). Workflow: `make e2e-up` → `make e2e-run` → fix → repeat → `make e2e-down` only after pass.
- **Every Python file must be covered by `make typecheck`.** New module → add to the mypy invocation in Makefile. Files inside an existing whole-directory target (`cli/`, `services/relay_core/`) are covered automatically.
- **Register new modules in `pyproject.toml`** (`testpaths`, `tool.ruff.src`, `known-first-party`, Makefile `lint:`/`typecheck:`). Exception: `cli/` is already whole-directory.
- After modifying any model in `services/shared/models.py`, `services/relay_core/notifier/models.py`, `services/relay_core/relay_models.py`, or `services/market_data/models/dividends.py`, run `make types`.
- **Always verify type safety by breaking it first.** After refactoring types, introduce a deliberate type error, run `make typecheck`, confirm it **fails**. Then revert. Never assume mypy catches something — prove it.
- **Avoid `dict[str, Any]` round-trips.** No `model_dump()` → `dict` → `Model(**data)`. Use explicit kwargs or `model_copy(update=...)`.
- **Prefer strict `Literal` over bare `str` on Pydantic models.** Use `BuySell`, `OrderType`, `AssetClass`, etc. when the value set is known. Fall back to `str` only when the external source is genuinely unbounded — document why.
- **No `# type: ignore` without justification.** Fix the root cause. Suppression is only acceptable with a reason: `# type: ignore[attr-defined] # ib_async.Foo has no stubs`.
- **Use `cast()` instead of `# type: ignore[arg-type]`.** Preserves downstream type-checking; `# type: ignore` silently disables it.
- **Use `@overload` for sentinel-default patterns.** Express the two signatures via `@overload` instead of `# type: ignore` on the return. Use `cast()` in the impl body.

## Pydantic Best Practices

- **`Field(default_factory=list)`** for mutable defaults — only when genuinely optional. Never bare `[]` or `{}`.
- **No defaults on always-populated fields.** A default makes the field optional in JSON Schema / TS (`fillCount?: number`). Defaults only for fields that are legitimately absent.
- **`ConfigDict(extra="forbid")`** on external-contract models (webhook payloads, API responses). Produces `additionalProperties: false`.
- **Docstrings claiming "never raises"** must match the impl. Wrap calls that can throw (e.g. `ET.fromstring()`) in try/except and return errors via the result tuple.

## Error Handling (MANDATORY)

- **Every error must produce a clear, actionable message.** Explain _what_ failed and _why_. Include context. Never raise generic "something went wrong".
- **API responses must never leak internal details.** Return structured error JSON with appropriate HTTP status. Never expose tracebacks, paths, or class names to callers. Log full exception server-side.
- **Isolate failures.** When dispatching to multiple backends/services, wrap each call in `try/except Exception`, log, continue. A single broken notifier must not crash the poll cycle.
- **Never silently swallow errors.** Every `except` must `log.exception(...)` or re-raise. Bare `except: pass` is never acceptable.
- **`log.exception()` for unexpected errors** (auto-includes traceback at ERROR). `log.error()` for known/expected failures where traceback is noise.
- **Distinguish recoverable from fatal.** Network timeouts → log, retry, skip. Missing config → fail fast with `raise SystemExit(msg)` or `die()`. Never limp along.
- **`SystemExit` must carry a descriptive message.** Never `raise SystemExit(1)` — callers that catch lose all context. Always include a reason.
- **Env var parsing must fail fast.** Wrap `int()`/`float()` in `try/except ValueError: raise SystemExit(...)`. Fall back only on _missing_ vars, never on _invalid_ values.
- **Validate at system boundaries, trust internally.** Validate at the entry point (API payloads, env vars, webhook data, Flex XML). Once validated, internal code does not re-validate.
- **Never assume a default for financial enum fields.** `BuySell.BUY if x == "buy" else BuySell.SELL` treats any non-buy value as SELL — including typos and nulls. Enumerate every valid value explicitly and raise on unknown.
- **`fee` is always positive (amount paid).** Industry standard. IBKR reports negative numbers; parsers normalize via `abs()`. Never store or forward negative fees.
- **Never silently drop rows with missing identifiers.** Report a parse error and skip explicitly. Don't fall through to a later guard where the drop is invisible.
- **HTTP handlers must catch and map exceptions.** Every aiohttp route handler must have a top-level `try/except` returning structured JSON 500.
- **Include context in error messages.** Bad: `"Failed to fetch Flex report"`. Good: `"Failed to fetch Flex report: query 12345 — HTTP 500 from IBKR"`.

## Reliability (MANDATORY)

- **Mark-after-notify, never before.** `mark_processed_batch()` runs ONLY AFTER `notify()` succeeds. A crash between mark and notify silently drops fills permanently. Run them sequentially in the same execution context (same thread or same `asyncio.to_thread` call).
- **Never separate mark from notify with an `await` boundary.** Keep atomic inside one synchronous block.
- **Replay mode is the exception.** `poll --replay N` intentionally skips dedup — by design for debugging.
- **SQLite commits must be explicit.** After any `INSERT`/`UPDATE`, call `conn.commit()` immediately.

## Concurrency Safety (MANDATORY)

- **Assume concurrency by default.** The relay is async (aiohttp). Any handler can be interrupted at an `await`. Before merging any code touching shared state, ask: "Can two callers interleave?"
- **Never use TOCTOU patterns with locks.** Do NOT check `lock.locked()` then `async with lock:`. The lock acquisition must BE the check. Use `asyncio.wait_for(lock.acquire(), timeout=0)` with `try/finally: lock.release()` to fail-fast, or accept `async with lock:` will queue. Applies to all shared-state guards.
- **Never share a `sqlite3.Connection` across threads.** Not thread-safe. With `asyncio.to_thread()`, either pass the connection into a single sync function that does all DB work in one thread, or use an `asyncio.Lock` to serialise.
- **Poller `to_thread` pattern: create connections inside the worker thread.** Do NOT create `sqlite3.Connection` on the event-loop thread and pass it into `asyncio.to_thread(poll_once, conn, ...)`. Instead, `poll_once()` creates thread-local connections via `init_dedup_db()` / `init_meta_db()`, closes them in `finally`.
- **Financial operations require extra scrutiny.** Order placement, money movement, account state — review for races, double-execution, partial failure, idempotency.
- **Use `asyncio.to_thread(func, *args)` to offload blocking calls**, never `loop.run_in_executor(None, func, *args)` unless you need a custom Executor.
- **Use `asyncio.get_running_loop()`, never `asyncio.get_event_loop()`** (deprecated since 3.10).
- **Schedule background tasks via `asyncio.get_running_loop().create_task(coro)`**, not `asyncio.ensure_future(coro)`. Retain the returned `Task` in a tracking set with `task.add_done_callback(set.discard)` to prevent GC mid-run.
- **Tasks stored in a dict keyed by external IDs must clean up on completion.** Wire `task.add_done_callback(cleanup)`. `cleanup(task)` pops the entry **only when the dict slot still points at the finishing task** (so a cancelled task whose slot has been replaced does not evict its replacement):

  ```python
  task = asyncio.create_task(...)
  task.add_done_callback(functools.partial(self._cleanup_task, key))
  self._tasks[key] = task

  def _cleanup_task(self, key: str, task: asyncio.Task) -> None:
      if self._tasks.get(key) is task:
          self._tasks.pop(key, None)
  ```

- **SQLite schema migrations must be PRAGMA-gated AND race-safe.** Two layers: (1) gate DDL behind a cheap `PRAGMA table_info(<table>)` read so steady-state stays read-only; (2) wrap the `ALTER TABLE` in `contextlib.suppress(sqlite3.OperationalError)` for the first-deploy race. Pattern:

  ```python
  cols = {row[1] for row in conn.execute("PRAGMA table_info(processed_fills)")}
  if "order_id" not in cols:
      with contextlib.suppress(sqlite3.OperationalError):
          conn.execute("ALTER TABLE processed_fills ADD COLUMN order_id TEXT")
  ```

## Dependency Management

- **Runtime deps** use exact pins (`==`). Builds must be reproducible.
- **`requirements-dev.txt`** contains only dev-only tools (mypy, pytest, ruff). Runtime deps belong in service `requirements.txt` exclusively. Both installed together — never duplicate.
- **New dep** → pin immediately. Runtime: exact (`==`) in service file. Dev: major-version (`>=X,<X+1`).
- **All services pinning the same dep must use the same version.** Check: `grep -r 'aiohttp==' services/*/requirements.txt`.

## Code Style

- Python: `logging` module, f-strings, `aiohttp` for async HTTP, `httpx` for sync HTTP in the poller engine.
- CLI: stdlib only. No third-party deps. Lazy dispatch via `importlib.import_module`.
- Terraform: secrets marked `sensitive = true` in `variables.tf`.

## Sibling Project: ibkr_bridge

This project and its sibling `ibkr_bridge` share the same CLI deploy/destroy/sync infrastructure pattern. Any change to `cli/core/deploy.py`, `cli/core/destroy.py`, or `cli/core/sync.py` here must be mirrored in `ibkr_bridge`, and vice versa.
