"""IBKR Flex Poller — entry point."""

import asyncio
import logging
import sys

from listener import (
    ListenerConfig,
    build_listener_config,
    is_listener_enabled,
    start_listener,
)
from notifier import load_notifiers
from notifier.base import BaseNotifier
from poller import (
    get_flex_query_id,
    get_flex_token,
    get_poll_interval,
    init_dedup_db,
    init_meta_db,
    poll_once,
    prune_old,
)
from poller_routes import start_api_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("poller")


async def _poll_loop(
    poll_lock: asyncio.Lock,
    notifiers: list[BaseNotifier],
) -> None:
    """Run poll_once in a thread at regular intervals."""
    poll_interval = get_poll_interval()
    while True:
        try:
            async with poll_lock:
                await asyncio.to_thread(poll_once, notifiers=notifiers)
        except Exception:
            log.exception("Poll cycle failed")

        log.debug("Next poll in %ds", poll_interval)
        await asyncio.sleep(poll_interval)


async def _run_listener(
    cfg: ListenerConfig,
    notifiers: list[BaseNotifier],
) -> None:
    """Run the listener, isolating runtime failures from the poller."""
    try:
        await start_listener(cfg, notifiers)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Listener crashed — poller continues unaffected")


async def amain() -> None:
    """Continuous polling loop with HTTP API for on-demand polls."""
    get_flex_token()  # fail fast if missing
    get_flex_query_id()  # fail fast if missing
    poll_interval = get_poll_interval()

    log.info("IBKR Flex Poller starting (poll every %ds)", poll_interval)

    notifiers = load_notifiers()
    if not notifiers:
        log.info("No notifiers configured — running in dry-run mode")

    # One-time startup prune on the main thread
    dedup_conn = init_dedup_db()
    prune_old(dedup_conn)
    dedup_conn.close()

    poll_lock = asyncio.Lock()

    await start_api_server(poll_lock, notifiers)

    # Start listener if enabled (runs as a background asyncio task).
    # Config is built eagerly here so bad config (SystemExit) kills the
    # process immediately — SystemExit inside an asyncio task is swallowed.
    if is_listener_enabled():
        listener_cfg = build_listener_config()
        log.info(
            "Listener enabled (exec_events=%s, debounce=%dms)",
            listener_cfg.exec_events_enabled, listener_cfg.debounce_ms,
        )
        asyncio.create_task(_run_listener(listener_cfg, notifiers))  # noqa: RUF006 — fire-and-forget by design; poller continues if listener crashes

    await _poll_loop(poll_lock, notifiers)


def main_once() -> None:
    """Single on-demand poll, then exit."""
    get_flex_token()  # fail fast if missing
    get_flex_query_id()  # fail fast if missing

    debug = "--debug" in sys.argv
    replay = 0
    if "--replay" in sys.argv:
        idx = sys.argv.index("--replay")
        replay = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 0
    notifiers = load_notifiers()
    dedup_conn = init_dedup_db()
    meta_conn = init_meta_db()
    orders = poll_once(dedup_conn, meta_conn, debug=debug, replay=replay, notifiers=notifiers)
    dedup_conn.close()
    meta_conn.close()
    n = len(orders) if isinstance(orders, list) else 0
    print(f"Done — {n} new trade(s) processed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        main_once()
    else:
        asyncio.run(amain())
