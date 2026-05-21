"""GET /v1/market-data/dividends/upcoming — fetch upcoming dividend info."""

import asyncio
import logging

from aiohttp import web
from pydantic import ValidationError

from market_data.adapters import get_adapter
from market_data.errors import AppError, ErrorCode
from market_data.models.dividends import DividendsUpcomingQuery, DividendsUpcomingResponse

log = logging.getLogger(__name__)


async def handle_dividends_upcoming(request: web.Request) -> web.Response:
    """GET /v1/market-data/dividends/upcoming?symbol=AAPL,GOOG&target=yahoo"""
    try:
        query_data = dict(request.rel_url.query)
        if "symbol" in request.rel_url.query:
            symbols = request.rel_url.query.getall("symbol")
            query_data["symbol"] = symbols if len(symbols) > 1 else symbols[0]
        query = DividendsUpcomingQuery.model_validate(query_data)
    except ValidationError as exc:
        return web.json_response({"error": exc.errors(include_url=False)}, status=422)

    adapter = get_adapter(query.target)
    if adapter is None:
        raise AppError(
            f"No adapter registered for target {query.target!r}",
            ErrorCode.INTERNAL_ERROR,
        )

    loop = asyncio.get_running_loop()
    items, errors = await loop.run_in_executor(
        None, adapter.get_dividends_upcoming, query.symbol
    )

    resp = DividendsUpcomingResponse(data=items, errors=errors)
    return web.json_response(resp.model_dump())
