"""IBKR Flex Poller — entry point."""

import asyncio
import logging
import sqlite3
import sys

from poller import FLEX_TOKEN, FLEX_QUERY_ID, POLL_INTERVAL, TARGET_WEBHOOK_URL
from poller import init_db, poll_once, prune_old
from routes import start_api_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("poller")


async def _poll_loop(
    db_conn: sqlite3.Connection,
    poll_lock: asyncio.Lock,
) -> None:
    """Run poll_once in a thread at regular intervals."""
    while True:
        try:
            async with poll_lock:
                await asyncio.to_thread(poll_once, db_conn)
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
    if not TARGET_WEBHOOK_URL:
        log.info("No TARGET_WEBHOOK_URL — running in dry-run mode")

    db_conn = init_db()
    prune_old(db_conn)

    poll_lock = asyncio.Lock()

    await start_api_server(db_conn, poll_lock)
    await _poll_loop(db_conn, poll_lock)


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
    conn = init_db()
    orders = poll_once(conn, debug=debug, replay=replay)
    conn.close()
    n = len(orders) if isinstance(orders, list) else 0
    print(f"Done — {n} new trade(s) processed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        main_once()
    else:
        asyncio.run(amain())
