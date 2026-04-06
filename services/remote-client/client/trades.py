"""Trades namespace — list session and completed trades."""

import logging
from decimal import Decimal
from typing import cast

from ib_async import IB
from ib_async import Trade as IBTrade
from ib_async.objects import Fill as IBFill

from models_remote_client import (
    FillDetail,
    ListTradesResponse,
    SecType,
    TimeInForce,
    TradeDetail,
)

log = logging.getLogger("ib-client")

# ib_async uses a sentinel float for "unset". Treat it as None.
_UNSET = 1.7976931348623157e308


def _lmt_price(value: float | Decimal | None) -> float | None:
    if value is None or value == _UNSET:
        return None
    return float(value)


def _map_fill(fill: IBFill) -> FillDetail:
    """Convert an ib_async Fill to FillDetail."""
    ex = fill.execution
    cr = fill.commissionReport
    return FillDetail(
        execId=ex.execId,
        time=ex.time.isoformat() if ex.time else "",
        exchange=ex.exchange,
        side=ex.side,
        shares=ex.shares,
        price=ex.price,
        commission=cr.commission,
        commissionCurrency=cr.currency,
        realizedPNL=cr.realizedPNL,
    )


def _map_trade(trade: IBTrade) -> TradeDetail:
    """Convert an ib_async Trade to TradeDetail."""
    o = trade.order
    c = trade.contract
    s = trade.orderStatus
    return TradeDetail(
        orderId=o.permId,
        action=o.action,
        totalQuantity=o.totalQuantity,
        orderType=o.orderType,
        lmtPrice=_lmt_price(o.lmtPrice),
        tif=cast(TimeInForce, o.tif),
        symbol=c.symbol,
        secType=cast(SecType, c.secType),
        exchange=c.exchange,
        currency=c.currency,
        status=s.status,
        filled=s.filled,
        remaining=s.remaining,
        avgFillPrice=s.avgFillPrice,
        fills=[_map_fill(f) for f in trade.fills],
    )


class TradesNamespace:
    """Trade listing operations against IB Gateway."""

    def __init__(self, ib: IB) -> None:
        self._ib = ib

    async def list(self) -> ListTradesResponse:
        """Return all session trades plus completed orders.

        Combines ``ib.trades()`` (session cache) with
        ``reqCompletedOrders()`` (survives reconnections) and
        deduplicates by ``orderId`` (the permanent order ID).
        """
        session_trades = self._ib.trades()

        completed = await self._ib.reqCompletedOrdersAsync(apiOnly=False)

        seen_perm_ids: set[int] = set()
        merged: list[TradeDetail] = []

        for t in session_trades:
            detail = _map_trade(t)
            seen_perm_ids.add(detail.orderId)
            merged.append(detail)

        for t in completed:
            if t.order.permId not in seen_perm_ids:
                detail = _map_trade(t)
                seen_perm_ids.add(detail.orderId)
                merged.append(detail)

        log.debug(
            "Listed %d trades (%d session + %d completed, %d after dedup)",
            len(merged), len(session_trades), len(completed), len(merged),
        )

        return ListTradesResponse(trades=merged)
