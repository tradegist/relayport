"""IBKR Remote Client — entrypoint.

Starts the IB Gateway connection and HTTP API server.
"""

import asyncio
import logging
import os

from aiohttp import web

from client import IBClient
from routes import create_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("remote-client")

API_PORT = int(os.environ.get("API_PORT", "5000"))


async def amain() -> None:
    client = IBClient()

    log.info("IBKR Remote Client starting (mode=%s)", os.environ.get("TRADING_MODE", "paper"))

    await client.connect()

    client.ib.disconnectedEvent += client.on_disconnect

    # Start watchdog to detect stale connections
    asyncio.ensure_future(client.watchdog())

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
