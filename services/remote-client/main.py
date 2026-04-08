"""IBKR Remote Client — entrypoint.

Starts the IB Gateway connection and HTTP API server.
"""

import asyncio
import logging
import os
from pathlib import Path

from aiohttp import web

from client import IBClient
from client.listener import ListenerNamespace
from dedup import init_db
from notifier import load_notifiers
from rc_routes import create_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("remote-client")

API_PORT = int(os.environ.get("API_PORT", "5000"))
DEDUP_DB_PATH = os.environ.get("DEDUP_DB_PATH", "/data/dedup/fills.db")
DEBOUNCE_MS = int(os.environ.get("LISTENER_EVENT_DEBOUNCE_TIME", "0"))


async def amain() -> None:
    client = IBClient()

    log.info("IBKR Remote Client starting (mode=%s)", os.environ.get("TRADING_MODE", "paper"))

    await client.connect()

    client.ib.disconnectedEvent += client.on_disconnect

    # Start listener if enabled
    listener_flag = os.environ.get("LISTENER_ENABLED", "").lower()
    if listener_flag and listener_flag not in ("0", "false", "no"):
        db_path = Path(DEDUP_DB_PATH)
        db = init_db(db_path)
        notifiers = load_notifiers()
        client.listener = ListenerNamespace(
            client.ib, notifiers, db, debounce_ms=DEBOUNCE_MS,
        )
        client.listener.start()
        log.info("Listener enabled — subscribed to trade events")

    # Start watchdog to detect stale connections
    watchdog_task = asyncio.ensure_future(client.watchdog())
    client._background_tasks.add(watchdog_task)
    watchdog_task.add_done_callback(client._background_tasks.discard)

    log.info("Remote client ready. Starting HTTP API on port %d ...", API_PORT)

    app = create_routes(client)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()

    log.info("HTTP API listening on 0.0.0.0:%d", API_PORT)

    while True:
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(amain())
