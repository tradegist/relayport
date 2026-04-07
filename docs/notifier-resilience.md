# Notifier Resilience — Technical Specification

> Follow-up task for both `ibkr_relay` and `kraken_relay`.
> The notifier package (`services/notifier/`) is identical in both projects — apply all changes to both.

## Problem

`notify()` currently swallows all exceptions per-backend. If `WebhookNotifier.send()` fails (network timeout, 5xx, DNS error), the exception is caught and logged, but `mark_processed_batch()` still runs. The fill is permanently marked as processed even though the webhook was never delivered. No retry is attempted.

This violates the mark-after-notify reliability rule: fills should only be marked processed when at least one notifier succeeds.

**Two data-loss vectors on failed notify:**

1. **Dedup mark** — `mark_processed_batch()` runs unconditionally after `notify()`. Once marked, the fill is permanently skipped by both the poller (dedup check) and the listener (dedup check). The webhook was never delivered, but the fill is recorded as processed. Unrecoverable without manual DB intervention.

2. **Watermark advancement** — both pollers update their timestamp watermark after marking fills. The kraken poller calls `_set_watermark()` to the latest `time` field; the ibkr poller calls `set_last_poll_ts()` to the latest fill timestamp. If the watermark advances past fills that were never delivered, those fills won't even be _fetched_ on the next poll cycle — they fall behind the watermark window. This is worse than the dedup mark: even replaying can't recover fills that are no longer returned by the API query.

## Current Architecture

```
notify(notifiers, payload)
  └─ for each notifier:
       try: notifier.send(payload)
       except: log.exception(...)   ← swallowed, no retry, no failure signal

mark_processed_batch(conn, ids)     ← always runs (data-loss vector 1)
set_watermark(conn, max_timestamp)  ← always runs (data-loss vector 2)
```

**Callers:**

- `kraken_relay` listener: `_handle_message()` — synchronous call in WS message handler
- `kraken_relay` poller: `poll_once()` — synchronous, run via `asyncio.to_thread()`
- `ibkr_relay` poller: `poll_once()` — synchronous, run via `asyncio.to_thread()`
- `ibkr_relay` listener: `_send_and_mark()` — synchronous closure, run via `asyncio.to_thread()`

## Target Architecture

```
notify(notifiers, payload)
  └─ for each notifier:
       try: notifier.send(payload)       ← send() now retries internally
       except: record failure
  └─ if ALL notifiers failed: raise NotificationError(failures)
  └─ if at least 1 succeeded: return (log warnings for failed ones)

# Caller decides:
if notify succeeded (at least 1 backend delivered):
    mark_processed_batch(conn, ids)        ← only on success
    set_watermark(conn, max_timestamp)     ← only on success
else:
    # NotificationError propagates — fill stays unprocessed,
    # watermark stays unchanged, fills re-fetched + retried next cycle
```

## Changes

### 1. New env vars

| Variable                | Default | Max     | Description                                                         |
| ----------------------- | ------- | ------- | ------------------------------------------------------------------- |
| `NOTIFY_RETRIES`        | `0`     | `5`     | Number of retry attempts per notifier backend after initial failure |
| `NOTIFY_RETRY_DELAY_MS` | `1000`  | `30000` | Delay between retries in milliseconds                               |

These are **global** (not per-backend). They apply identically to every configured notifier.

Suffix support: `NOTIFY_RETRIES_2` / `NOTIFY_RETRY_DELAY_MS_2` for `poller-2` (ibkr_relay). Read via `load_notifiers(suffix=...)` and stored on the notifier instance or passed through `notify()`.

### 2. `BaseNotifier` changes (`services/notifier/base.py`)

No changes to the ABC interface. `send()` contract stays the same: attempt delivery, raise on failure.

**Critical change:** remove the "never raise" language from the `send()` docstring. Backends MUST raise on delivery failure so the retry wrapper can catch and retry. The current `WebhookNotifier.send()` catches `httpx.HTTPError` and logs it — that catch must be removed so the exception propagates.

### 3. `WebhookNotifier.send()` changes (`services/notifier/webhook.py`)

**Before:**

```python
def send(self, payload: BaseModel) -> None:
    # ... build request ...
    try:
        resp = httpx.post(self._url, content=body, headers=headers, timeout=10.0)
        log.info("Webhook sent — status %d", resp.status_code)
    except httpx.HTTPError as exc:
        log.error("Webhook delivery failed: %s", exc)  # swallowed!
```

**After:**

```python
def send(self, payload: BaseModel) -> None:
    # ... build request ...
    resp = httpx.post(self._url, content=body, headers=headers, timeout=10.0)
    resp.raise_for_status()
    log.info("Webhook sent — status %d", resp.status_code)
```

- Remove the try/except around `httpx.post()` — let exceptions propagate.
- Add `resp.raise_for_status()` — treat 4xx/5xx as errors (currently a 500 response is silently accepted).
- Dry-run mode (no URL) still returns early without raising.

### 4. `notify()` changes (`services/notifier/__init__.py`)

**New exception class** (top of file):

```python
class NotificationError(Exception):
    """Raised when ALL notifier backends fail to deliver."""
    def __init__(self, failures: list[tuple[str, Exception]]) -> None:
        self.failures = failures
        names = ", ".join(name for name, _ in failures)
        super().__init__(f"All notifiers failed: {names}")
```

**New `notify()` implementation:**

```python
def notify(
    notifiers: list[BaseNotifier],
    payload: BaseModel,
    *,
    retries: int = 0,
    retry_delay_ms: int = 1000,
) -> None:
    """Dispatch payload to all configured notifiers.

    Each backend is retried up to `retries` times on failure.
    If at least one backend succeeds, return normally (log warnings for failures).
    If ALL backends fail, raise NotificationError — caller must NOT mark fills.
    """
    if not notifiers:
        log.info("No notifiers configured — skipping notification")
        return

    succeeded = 0
    failures: list[tuple[str, Exception]] = []

    for notifier in notifiers:
        last_exc: Exception | None = None
        for attempt in range(1 + retries):
            try:
                notifier.send(payload)
                succeeded += 1
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    delay_s = retry_delay_ms / 1000.0
                    log.warning(
                        "Notifier %s attempt %d/%d failed: %s — retrying in %.1fs",
                        type(notifier).__name__, attempt + 1, 1 + retries,
                        exc, delay_s,
                    )
                    time.sleep(delay_s)

        if last_exc is not None:
            log.error(
                "Notifier %s failed after %d attempt(s): %s",
                type(notifier).__name__, 1 + retries, last_exc,
            )
            failures.append((type(notifier).__name__, last_exc))

    if succeeded == 0:
        raise NotificationError(failures)

    if failures:
        names = ", ".join(name for name, _ in failures)
        log.warning(
            "%d/%d notifier(s) failed: %s — fills will be marked processed (partial success)",
            len(failures), len(notifiers), names,
        )
```

**Key behaviors:**

- `time.sleep()` for retry delay — all callers already run `notify()` in a synchronous context (directly or via `asyncio.to_thread`), so blocking is fine.
- Add `import time` to the file.
- The retry loop is per-backend. If backend A fails after 3 retries and backend B succeeds, fills are marked processed and a warning is logged for A.
- If ALL backends fail, `NotificationError` is raised — the caller's existing `try/except` handles it.

### 5. Reading retry config

In `load_notifiers()` — read and validate the env vars, return them alongside the notifier list. Two options:

**Option A (preferred): Store on module-level and pass through `notify()`.**

Add a helper:

```python
def load_retry_config(suffix: str = "") -> tuple[int, int]:
    """Read NOTIFY_RETRIES and NOTIFY_RETRY_DELAY_MS from env."""
    retries = int(os.environ.get(f"NOTIFY_RETRIES{suffix}", "0"))
    delay_ms = int(os.environ.get(f"NOTIFY_RETRY_DELAY_MS{suffix}", "1000"))
    if retries < 0 or retries > 5:
        raise SystemExit(f"NOTIFY_RETRIES{suffix} must be 0–5, got {retries}")
    if delay_ms < 0 or delay_ms > 30000:
        raise SystemExit(f"NOTIFY_RETRY_DELAY_MS{suffix} must be 0–30000, got {delay_ms}")
    return retries, delay_ms
```

Callers load config at startup and pass to `notify()`:

```python
notifiers = load_notifiers()
retries, retry_delay_ms = load_retry_config()
# ...
notify(notifiers, payload, retries=retries, retry_delay_ms=retry_delay_ms)
```

### 6. Caller changes

All callers have `mark_processed_batch()` and watermark updates after `notify()`. If `notify()` raises `NotificationError`, both must be skipped. Since `NotificationError` is a regular exception, the existing code structure already handles this — neither `mark_processed_batch()` nor the watermark update will execute because the exception propagates past them.

**Why watermark gating matters:** Both pollers advance a timestamp watermark after processing fills. If `notify()` fails but the watermark still advances, the next poll cycle starts _after_ the failed fills — they fall outside the query window and are never re-fetched. This is worse than the dedup mark: even replay mode can't recover fills that the API no longer returns. The watermark must only advance when at least one notifier succeeded.

**Verify each caller:**

| Caller          | File                               | Current behavior                                                    | Change needed                                                                                                                |
| --------------- | ---------------------------------- | ------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| kraken listener | `kraken_ws.py` `_handle_message()` | `notify()` → `mark_processed_batch()` inline                        | None — exception propagates, fill stays unprocessed, retried on next WS message or reconnect. No watermark in listener path. |
| kraken poller   | `poller/__init__.py` `poll_once()` | `notify()` → `mark_processed_batch()` → `_set_watermark()` inline   | None — exception propagates, `poll_loop` catches it, fill retried next cycle. Watermark stays unchanged.                     |
| ibkr poller     | `poller/__init__.py` `poll_once()` | `notify()` → `mark_processed_batch()` → `set_last_poll_ts()` inline | None — same as kraken poller. Watermark stays unchanged.                                                                     |
| ibkr listener   | `listener.py` `_send_and_mark()`   | `notify()` → `mark_processed_batch()` in closure                    | None — exception propagates from `asyncio.to_thread()`, caught by `_on_dispatch_done`. No watermark in listener path.        |

**All callers need:** pass `retries` and `retry_delay_ms` to `notify()`. This means threading the config from startup through to the call site (either via function parameters or storing on the class instance).

### 7. Env var additions

**`.env.example` (both projects):**

```env
# ── Notification retry ───────────────────────────────────────────────
# Retry attempts per notifier backend on failure (0 = no retries, max 5)
#NOTIFY_RETRIES=0
# Delay between retries in milliseconds (default: 1000, max: 30000)
#NOTIFY_RETRY_DELAY_MS=1000
```

**`docker-compose.yml` environment blocks** — add to poller, poller-2, and remote-client:

```yaml
NOTIFY_RETRIES: ${NOTIFY_RETRIES:-0}
NOTIFY_RETRY_DELAY_MS: ${NOTIFY_RETRY_DELAY_MS:-1000}
```

For poller-2 (ibkr_relay):

```yaml
NOTIFY_RETRIES: ${NOTIFY_RETRIES_2:-0}
NOTIFY_RETRY_DELAY_MS: ${NOTIFY_RETRY_DELAY_MS_2:-1000}
```

### 8. Test changes

**`test_webhook.py`:**

- Remove or update any test that expects `send()` to swallow errors.
- Add test: `send()` raises `httpx.HTTPStatusError` on 5xx response.
- Add test: `send()` raises `httpx.ConnectError` on network failure.
- Dry-run mode: `send()` still returns without raising.

**`test_notifier.py`:**

- Add test: `notify()` with 1 backend that fails → raises `NotificationError`.
- Add test: `notify()` with 2 backends, one fails, one succeeds → returns normally, logs warning.
- Add test: `notify()` retries on failure up to `retries` times.
- Add test: `notify()` with `retries=0` does not retry.
- Add test: `notify()` retry delay is respected (mock `time.sleep`).
- Add test: `notify()` with all backends failing after retries → raises `NotificationError` with failure details.
- Add test: `notify()` with empty notifier list → returns without error.

**Caller tests (optional but recommended):**

- Add test in each poller's `test_poller.py`: when `notify()` raises `NotificationError`, `mark_processed_batch()` is NOT called.

### 9. Documentation updates

- **README.md** (both projects): add `NOTIFY_RETRIES` and `NOTIFY_RETRY_DELAY_MS` to the env var table.
- **copilot-instructions** (both projects): update the Notifier Structure section to mention retry behavior.
- **project_blueprint.md**: update the notifier section with the retry/partial-success semantics.

## Implementation Order

1. `services/notifier/base.py` — update `send()` docstring
2. `services/notifier/webhook.py` — remove try/except, add `raise_for_status()`
3. `services/notifier/__init__.py` — add `NotificationError`, `load_retry_config()`, update `notify()`
4. `services/notifier/test_webhook.py` — update tests
5. `services/notifier/test_notifier.py` — add retry + partial success tests
6. Thread retry config through callers (startup → `notify()` call sites)
7. `.env.example`, `docker-compose.yml`, `docker-compose.test.yml` — add env vars
8. `make test && make typecheck && make lint` on both projects
9. README, copilot-instructions, blueprint updates

## Risks

- **Retry delay blocks the thread.** For the pollers this is fine (already in `to_thread`). For the ibkr listener, `_send_and_mark` also runs in `to_thread`. For the kraken listener, `_handle_message()` is synchronous but called from the async WS loop — a 5s total retry delay (5 retries × 1s) would block WS message processing. Consider wrapping kraken listener dispatch in `asyncio.to_thread()` as part of this work (or accept the tradeoff since poll interval is 5min and fills are rare).
- **`raise_for_status()` treats 4xx as errors.** A 400 (bad request) from the webhook consumer means the payload is invalid — retrying won't help. Consider only retrying on 5xx/timeout, not 4xx. Implementation: catch `httpx.HTTPStatusError`, check `exc.response.status_code >= 500`, re-raise only for 5xx. For 4xx, log error and raise immediately (no retry).
