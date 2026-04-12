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

from . import BrokerRelay
from .context import init_relays
from .listener_engine import start_listener
from .poller_engine import init_dedup_db, poll_once, prune_old
from .registry import load_relays
from .routes import start_api_server

log = logging.getLogger("relays")


def configure_logging() -> None:
    """Set up root logging and redaction filters.

    Called once from ``amain()`` so the level respects env vars at startup
    rather than at import time.
    """
    from relays.ibkr.flex_fetch import _RedactTokenFilter

    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Redact sensitive query-param tokens (e.g. Flex ``t=``) from all log output.
    # The filter lives in flex_fetch; we install it on every root handler so it
    # catches records propagated from child loggers like httpx._client.
    redact = _RedactTokenFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)


async def _poll_loop(
    relay: BrokerRelay,
    poller_index: int,
) -> None:
    """Run poll_once in a thread at regular intervals for one poller."""
    config = relay.poller_configs[poller_index]
    poll_lock = relay.poll_locks[poller_index]
    relay_log = logging.getLogger(f"poller.{relay.name}")
    if poller_index > 0:
        relay_log = logging.getLogger(f"poller.{relay.name}.{poller_index}")

    while True:
        try:
            async with poll_lock:
                await asyncio.to_thread(
                    poll_once,
                    relay_name=relay.name,
                    poller_index=poller_index,
                )
        except Exception:
            relay_log.exception("Poll cycle failed")

        relay_log.debug("Next poll in %ds", config.interval)
        await asyncio.sleep(config.interval)


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

    if relays:
        # One-time startup prune
        dedup_conn = init_dedup_db()
        prune_old(dedup_conn)
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
