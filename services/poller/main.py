"""IBKR Flex Poller — entry point."""

import asyncio
import logging
import sys

from notifier import load_notifiers
from notifier.base import BaseNotifier
from poller import (
    FLEX_QUERY_ID,
    FLEX_TOKEN,
    POLL_INTERVAL,
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
    while True:
        try:
            async with poll_lock:
                await asyncio.to_thread(poll_once, notifiers=notifiers)
        except Exception:
            log.exception("Poll cycle failed")

        log.debug("Next poll in %ds", POLL_INTERVAL)
        await asyncio.sleep(POLL_INTERVAL)


async def amain() -> None:
    """Continuous polling loop with HTTP API for on-demand polls."""
    if not FLEX_TOKEN or not FLEX_QUERY_ID:
        log.error("IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID must be set")
        raise SystemExit(1)

    log.info("IBKR Flex Poller starting (poll every %ds)", POLL_INTERVAL)

    notifiers = load_notifiers()
    if not notifiers:
        log.info("No notifiers configured — running in dry-run mode")

    # One-time startup prune on the main thread
    dedup_conn = init_dedup_db()
    prune_old(dedup_conn)
    dedup_conn.close()

    poll_lock = asyncio.Lock()

    await start_api_server(poll_lock, notifiers)
    await _poll_loop(poll_lock, notifiers)


def main_once() -> None:
    """Single on-demand poll, then exit."""
    if not FLEX_TOKEN or not FLEX_QUERY_ID:
        log.error("IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID must be set")
        raise SystemExit(1)

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
