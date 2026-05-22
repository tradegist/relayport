"""Market data service entry point."""

import asyncio
import logging
import os
from typing import get_args

from market_data.adapters import known_targets, register
from market_data.adapters.yahoo import YahooAdapter
from market_data.models.dividends import MarketDataTarget
from market_data.routes.app import start_api_server
from market_data.routes.middlewares import validate_api_token


def configure_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _validate_registry() -> None:
    registered = set(known_targets())
    missing = [t for t in get_args(MarketDataTarget) if t not in registered]
    if missing:
        raise SystemExit(f"Adapters not registered for targets: {missing}")


async def amain() -> None:
    configure_logging()
    validate_api_token()
    register("yahoo", YahooAdapter)
    _validate_registry()
    await start_api_server()
    await asyncio.Event().wait()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
