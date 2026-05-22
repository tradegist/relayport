# `services/` — Shared rules across all services

Test conventions and the three-location model layout. Service-specific rules live in each subdirectory's `CLAUDE.md`.

## Models (Three Locations)

| File | Domain | Contains |
|---|---|---|
| `services/shared/models.py` | CommonFill primitives | `Fill`, `Trade`, `OptionContract`, `BuySell`, `AssetClass`, `OrderType`, `Source`, `RelayName` |
| `services/relay_core/notifier/models.py` | Notifier payload (outbound) | `WebhookPayloadTrades`, `WebhookPayload` |
| `services/relay_core/relay_models.py` | Relay API (outbound) | Re-exports notifier payload + `RunPollResponse`, `HealthResponse` |

- **`services/shared/models.py`** defines the primitives. The `__init__.py` barrel re-exports so `from shared import Fill` works.
- **`services/relay_core/notifier/models.py`** is the authoritative home for outbound webhook payload contracts. Add new payload variants here.
- **`services/shared/utilities.py`** contains the single internal helper `aggregate_fills` (orderId-grouped VWAP/cost/fee aggregation). Per-relay normalisation helpers (`normalize_order_type`, `normalize_asset_class`) live in each adapter package (e.g. `services/relays/ibkr/utilities.py`, `services/relays/kraken/ws_parser.py`), not in `shared/`. Engines dedup directly on `fill.execId` — there is no `_dedup_id()` helper.
- **Model shims only re-export models and types.** Utility functions must be imported directly from the owning module: `from shared import aggregate_fills`. Never re-export functions through model shims.
- All external-contract models use `ConfigDict(extra="forbid")` for strict validation.
- After modifying any of the three model files, run `make types` to regenerate TypeScript + Python type packages.

## Test File Convention

- **Unit tests are colocated** next to the source file: `flex_parser.py` → `test_flex_parser.py`, `registry.py` → `test_registry.py`.
- **E2E tests live in `tests/e2e/`** within each service.
- **`make test`** runs unit tests. **`make e2e-run`** runs E2E tests (requires Docker stack). **`make lint`** runs ruff. All must pass before deploying.
- **Import `unittest.mock` as a submodule, not via `from unittest import mock`.** Use `import unittest` and `import unittest.mock` together. Reference as `unittest.mock.patch`, `unittest.mock.MagicMock`. Avoids the mixed-import lint warning.
- **Always scope `unittest.mock.patch`.** Never `patch.start()` at module level without `patch.stop()` — leaks into every later test module. Use:
  - **`setUpModule()` / `tearDownModule()`** for module-wide patches.
  - **`self.addCleanup(patcher.stop)`** in `setUp()` for class-scoped.
  - **`with patch(...):`** inside a test for single-test.
  - **`@patch(...)`** decorator for single-test or single-class.
- **Use `setUpModule()` / `tearDownModule()` for env var overrides.** Save originals, restore on tear-down. Never mutate `os.environ` at module level without cleanup. Pattern:

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

  Both are called automatically by pytest/unittest. Prefer this over `mock.patch.dict(os.environ, ...)` for module-wide overrides. For single-test changes, use `with mock.patch.dict(os.environ, ...):`.

- **Avoid reading env vars at module level in production code.** Module-level reads bake values at import time, forcing tests to set vars before imports. Defer to a factory function (`create_app()`) or constructor so `setUpModule()` works normally.
- **No cross-test dependencies.** Every test must be self-contained. Pytest does not guarantee execution order.
- **Avoid `time.sleep()` in unit tests.** Wall-clock sleeps slow `make test` and CI. Control time instead:
  - **Time-based DB queries**: insert rows with `datetime('now', '-5 seconds')` rather than `datetime('now')` + sleep.
  - **Time-based logic**: extract the clock behind `_now()` / `time.monotonic()` and mock it, or pass an injectable clock.
  - **Async timer/debounce code**: assert on task identity/state immediately. When a real timer fire must be exercised, use the smallest interval (`debounce_ms=20`) and an `await asyncio.sleep(0.10)` — still finish in <1 s.
  - The only acceptable `time.sleep()` is E2E tests waiting on a Docker `HEALTHCHECK`. In a unit test, essentially never.
- **E2E conftest fixtures must use `yield` with a context manager.** Never `return httpx.Client(...)` — leaks sockets. Use `with httpx.Client(...) as client: yield client`. Scope to `session`.

## E2E Testing

- E2E tests run against a local Docker stack defined by `docker-compose.test.yml` (relays + debug, no Caddy).
- Credentials live in `.env.test` (gitignored). Template: `env_examples/env.test`.
- `make e2e` starts the stack, runs pytest, tears down. Always cleans up.
- `make e2e-up` / `make e2e-down` for manual stack management during debugging.
- `make e2e-run` restarts `relays` and `debug` containers (to pick up code changes from volume mounts), then runs E2E tests. Safe to call repeatedly — no rebuild needed.
- Test relays service runs on `localhost:15011` with hardcoded token `test-token`.

### E2E Conftest Pattern

The E2E conftest (`services/relay_core/tests/e2e/conftest.py`) uses a two-tier preflight:

- **`_stack_preflight`** — `scope="session"`, `autouse=True`. Hits `/health` on relays. `pytest.exit()` if unreachable (hard failure).
- **`_bridge_preflight`** — `scope="session"`, on-demand (requested by listener tests via `bridge_api`). Checks `LISTENER_ENABLED`, bridge creds, bridge reachability. `pytest.skip()` if any prereq missing (soft skip).

### Listener E2E Tests

- Listener E2E tests are **opt-in** — require a running ibkr_bridge local stack and `LISTENER_ENABLED=true` in `.env.test`.
- Preflight skips (not fails) when `LISTENER_ENABLED` is unset, bridge creds missing, or bridge unreachable.
- E2E conftest loads `.env.test` directly via a stdlib `_load_env_test()` helper (no `python-dotenv` dependency).
- Required `.env.test` vars: `IBKR_BRIDGE_WS_URL`, `IBKR_BRIDGE_API_BASE_URL`, `IBKR_BRIDGE_API_TOKEN`.
