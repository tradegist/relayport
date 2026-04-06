"""Listener namespace — subscribe to ib_async trade events and fire webhooks."""

import asyncio
import logging

from ib_async import IB
from ib_async import Trade as IBTrade
from ib_async.objects import CommissionReport
from ib_async.objects import Fill as IBFill

from models_poller import BuySell, Source, Trade, WebhookPayload
from notifier.base import BaseNotifier

log = logging.getLogger("ib-listener")

_SIDE_MAP: dict[str, BuySell] = {
    "BOT": BuySell.BUY,
    "SLD": BuySell.SELL,
}


def _map_to_trade(trade: IBTrade, fill: IBFill, source: Source) -> Trade:
    """Convert an ib_async Trade + single Fill into a poller Trade."""
    ex = fill.execution
    cr = fill.commissionReport
    o = trade.order
    c = trade.contract

    side = _SIDE_MAP.get(ex.side)
    if side is None:
        log.warning("Unknown execution side %r, defaulting to BUY", ex.side)
        side = BuySell.BUY

    commission = 0.0
    commission_currency = ""
    realized_pnl = 0.0
    if cr and source == "commissionReportEvent":
        commission = cr.commission if cr.commission != 1.7976931348623157e308 else 0.0
        commission_currency = cr.currency or ""
        realized_pnl = cr.realizedPNL if cr.realizedPNL != 1.7976931348623157e308 else 0.0

    return Trade(
        source=source,
        ibExecId=ex.execId,
        execIds=[ex.execId],
        fillCount=1,
        orderId=str(o.permId),
        buySell=side,
        quantity=ex.shares,
        price=ex.price,
        symbol=c.symbol,
        assetCategory=c.secType,
        exchange=ex.exchange,
        currency=c.currency,
        commission=commission,
        commissionCurrency=commission_currency,
        fifoPnlRealized=realized_pnl,
        dateTime=ex.time.isoformat() if ex.time else "",
        accountId=ex.acctNumber if hasattr(ex, "acctNumber") else "",
    )


class ListenerNamespace:
    """Subscribes to ib_async trade events and dispatches webhooks."""

    def __init__(self, ib: IB, notifiers: list[BaseNotifier]) -> None:
        self._ib = ib
        self._notifiers = notifiers

    def start(self) -> None:
        """Subscribe to execution and commission report events."""
        self._ib.execDetailsEvent += self._on_exec_details
        self._ib.commissionReportEvent += self._on_commission_report
        log.info("Listener subscribed to execDetailsEvent + commissionReportEvent")

    def _on_exec_details(self, trade: IBTrade, fill: IBFill) -> None:
        mapped = _map_to_trade(trade, fill, "execDetailsEvent")
        log.info(
            "execDetailsEvent: %s %s %s @ %.4f (execId=%s)",
            mapped.buySell.value, mapped.quantity, mapped.symbol,
            mapped.price, mapped.ibExecId,
        )
        self._dispatch(mapped)

    def _on_commission_report(
        self, trade: IBTrade, fill: IBFill, report: CommissionReport,
    ) -> None:
        mapped = _map_to_trade(trade, fill, "commissionReportEvent")
        log.info(
            "commissionReportEvent: %s %s %s commission=%.4f (execId=%s)",
            mapped.buySell.value, mapped.quantity, mapped.symbol,
            mapped.commission, mapped.ibExecId,
        )
        self._dispatch(mapped)

    def _dispatch(self, trade: Trade) -> None:
        """Fire webhook in a background thread (non-blocking)."""
        from notifier import notify

        payload = WebhookPayload(trades=[trade], errors=[])
        task = asyncio.ensure_future(asyncio.to_thread(notify, self._notifiers, payload))
        task.add_done_callback(self._on_dispatch_done)

    @staticmethod
    def _on_dispatch_done(task: asyncio.Task[None]) -> None:
        """Log errors from background webhook dispatch."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.exception("Webhook dispatch failed", exc_info=exc)
