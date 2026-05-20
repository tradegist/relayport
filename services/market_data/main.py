"""Market data service entry point."""

import asyncio
import logging
import os

from market_data.adapters import register
from market_data.adapters.yahoo import YahooAdapter
from market_data.routes.app import start_api_server


def configure_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def amain() -> None:
    configure_logging()
    register("yahoo", YahooAdapter)
    await start_api_server()
    await asyncio.Event().wait()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
