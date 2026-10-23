"""Relay service entry point — multi-relay poller + listener orchestrator.

Reads RELAYS env var, loads each adapter via the registry, then:
- Starts an HTTP API server (health + per-relay poll endpoints)
- Starts a poll loop per PollerConfig
- Starts a WS listener per relay (if enabled)
"""

import asyncio
import logging
import os
import sys

from . import BrokerRelay, StartupContext
from .alerter import send_alert
from .context import init_relays
from .dedup import get_retention_days
from .health import OutageAlert, OutageTracker, format_duration
from .listener_engine import start_listener
from .notifier import NotificationError
from .poller_engine import (
    FetchConfigError,
    FetchError,
    init_dedup_db,
    poll_once,
    prune_old,
)
from .registry import load_relays
from .routes import start_api_server

log = logging.getLogger("relays")

# After a fetch the broker rejected outright (FetchConfigError: expired
# token, invalid query), wait this long instead of the poll interval. More
# failed attempts cannot succeed and can arm broker lockouts — IBKR answers
# [1025] after ~10 consecutive failures, which then masks a fixed token.
# A redeploy with the fix restarts the poller immediately.
NEEDS_ACTION_PAUSE_S = 2 * 3600


def configure_logging() -> None:
    """Set up root logging level and format.

    Called once from ``amain()`` so the level respects env vars at startup
    rather than at import time.  Relay-specific log filters (e.g. token
    redaction) are registered later via the startup lifecycle.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def _poll_loop(
    relay: BrokerRelay,
    poller_index: int,
) -> None:
    """Run poll_once in a thread at regular intervals for one poller.

    Every cycle reports to the poller's ``OutageTracker``, which emails the
    operator once the broker fetch has kept failing for
    ``relay.outage_alert_after_s``.
    """
    config = relay.poller_configs[poller_index]
    poll_lock = relay.poll_locks[poller_index]
    relay_log = logging.getLogger(f"poller.{relay.name}")
    if poller_index > 0:
        relay_log = logging.getLogger(f"poller.{relay.name}.{poller_index}")
    component = "poller"
    if len(relay.poller_configs) > 1:
        component = f"poller {poller_index + 1}"
    outage = OutageTracker(relay.name, component, relay.outage_alert_after_s)

    while True:
        alert: OutageAlert | None
        delay = config.interval
        try:
            async with poll_lock:
                await asyncio.to_thread(
                    poll_once,
                    relay_name=relay.name,
                    poller_index=poller_index,
                )
        except FetchError as exc:
            relay_log.error("Poll fetch failed: %s", exc)
            needs_action = isinstance(exc, FetchConfigError)
            if needs_action:
                delay = max(config.interval, NEEDS_ACTION_PAUSE_S)
                relay_log.warning(
                    "Broker rejected the configuration — pausing this poller for %s"
                    " (redeploy with the fix to resume at once)",
                    format_duration(delay),
                )
            alert = outage.record_failure(
                str(exc), needs_action=needs_action,
                retry_in_s=delay if needs_action else None,
            )
        except NotificationError:
            # The broker fetch worked — delivery failures are alerted by
            # the notifier itself, per destination.
            relay_log.exception("Poll cycle failed")
            alert = outage.record_success()
        except Exception as exc:
            relay_log.exception("Poll cycle failed")
            # Type only: an arbitrary exception message may carry a URL
            # with credentials, and the alert leaves the host.
            alert = outage.record_failure(
                f"Poll cycle crashed with {type(exc).__name__} — see relay logs",
            )
        else:
            alert = outage.record_success()
        if alert is not None:
            await asyncio.to_thread(
                send_alert, subject=alert.subject, body=alert.body, key=alert.key,
            )

        relay_log.debug("Next poll in %ds", delay)
        await asyncio.sleep(delay)


async def _run_listener(relay: BrokerRelay) -> None:
    """Run the WS listener, isolating crashes from pollers."""
    if relay.listener_config is None:
        return
    try:
        await start_listener(relay_name=relay.name)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("[%s] Listener crashed — pollers continue", relay.name)


async def amain() -> None:
    """Load all relays, start API server, pollers, and listeners."""
    configure_logging()
    relays = load_relays()
    init_relays(relays)
    if not relays:
        log.info("No relays configured (RELAYS is empty) — running API server only")

    # Run relay startup lifecycle — collect and apply cross-cutting hooks
    # (e.g. log filters) registered by individual adapters.
    ctx = StartupContext()
    for relay in relays:
        if relay.on_start is not None:
            relay.on_start(ctx)
    ctx.apply()

    if relays:
        # One-time startup prune
        dedup_conn = init_dedup_db()
        prune_old(dedup_conn, days=get_retention_days())
        dedup_conn.close()

    # Initialize per-poller locks before the API server so handle_poll()
    # always sees them (avoids lockless on-demand polls during startup).
    for relay in relays:
        relay.poll_locks = [asyncio.Lock() for _ in relay.poller_configs]

    await start_api_server(relays)

    # Start poll loops + listeners
    for relay in relays:
        for idx in range(len(relay.poller_configs)):
            asyncio.create_task(  # noqa: RUF006
                _poll_loop(relay, idx),
            )
            log.info(
                "[%s] Poller %d started (interval=%ds)",
                relay.name, idx, relay.poller_configs[idx].interval,
            )

        if relay.listener_config is not None:
            asyncio.create_task(  # noqa: RUF006
                _run_listener(relay),
            )
            log.info(
                "[%s] Listener started (debounce=%dms)",
                relay.name, relay.listener_config.debounce_ms,
            )

    # Keep the main coroutine alive
    await asyncio.Event().wait()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    if "--help" in sys.argv:
        print("Usage: python -m relay_core.main")
        print("  Starts the multi-relay service (pollers + listeners + API)")
        sys.exit(0)
    main()
