"""Listener namespace — subscribe to ib_async trade events and fire webhooks.

Supports optional debouncing: when ``debounce_ms > 0``, rapid partial fills
for the same ``orderId`` are collected in memory and aggregated into a single
webhook after a quiet period of ``debounce_ms`` milliseconds.
"""

import asyncio
import logging
import sqlite3
from typing import Any

from ib_async import IB
from ib_async import Trade as IBTrade
from ib_async.objects import CommissionReport
from ib_async.objects import Fill as IBFill

from dedup import get_processed_ids, is_processed, mark_processed_batch, prune
from models_poller import (
    BuySell,
    Fill,
    Source,
    Trade,
    WebhookPayload,
)
from notifier import notify
from notifier.base import BaseNotifier
from shared import aggregate_fills

log = logging.getLogger("ib-listener")

_SIDE_MAP: dict[str, BuySell] = {
    "BOT": BuySell.BUY,
    "SLD": BuySell.SELL,
}


_UNSET = 1.7976931348623157e308  # ib_async sentinel for unset floats


def _map_to_fill(trade: IBTrade, fill: IBFill, source: Source) -> Fill:
    """Convert an ib_async Trade + single Fill into a CommonFill."""
    ex = fill.execution
    cr = fill.commissionReport
    o = trade.order
    c = trade.contract

    side = _SIDE_MAP.get(ex.side)
    if side is None:
        raise ValueError(f"Unknown execution side: {ex.side!r}")

    commission = 0.0
    commission_currency = ""
    realized_pnl = 0.0
    if cr and source == "commissionReportEvent":
        commission = cr.commission if cr.commission != _UNSET else 0.0
        commission_currency = cr.currency or ""
        realized_pnl = cr.realizedPNL if cr.realizedPNL != _UNSET else 0.0

    raw: dict[str, Any] = {
        "ibExecId": ex.execId,
        "orderId": str(o.permId),
        "side": ex.side,
        "quantity": ex.shares,
        "price": ex.price,
        "symbol": c.symbol,
        "assetCategory": c.secType,
        "exchange": ex.exchange,
        "currency": c.currency,
        "commission": commission,
        "commissionCurrency": commission_currency,
        "fifoPnlRealized": realized_pnl,
        "dateTime": ex.time.isoformat() if ex.time else "",
        "accountId": ex.acctNumber if hasattr(ex, "acctNumber") else "",
    }

    return Fill(
        execId=ex.execId,
        orderId=str(o.permId),
        symbol=c.symbol,
        side=side,
        orderType=None,
        price=ex.price,
        volume=ex.shares,
        cost=0.0,
        fee=commission,
        timestamp=ex.time.isoformat() if ex.time else "",
        source=source,
        raw=raw,
    )


def _fill_to_trade(fill: Fill) -> Trade:
    """Wrap a single Fill in a 1-fill Trade for immediate dispatch."""
    return Trade(
        orderId=fill.orderId,
        symbol=fill.symbol,
        side=fill.side,
        orderType=fill.orderType,
        price=fill.price,
        volume=fill.volume,
        cost=fill.cost,
        fee=fill.fee,
        fillCount=1,
        execIds=[fill.execId],
        timestamp=fill.timestamp,
        source=fill.source,
        raw=fill.raw,
    )


class ListenerNamespace:
    """Subscribes to ib_async trade events and dispatches webhooks.

    When ``debounce_ms > 0``, ``commissionReportEvent`` fills are buffered
    per ``orderId`` and flushed after a quiet period.  When ``debounce_ms == 0``
    (default), each event dispatches immediately (legacy behaviour).
    """

    def __init__(
        self,
        ib: IB,
        notifiers: list[BaseNotifier],
        db: sqlite3.Connection,
        *,
        debounce_ms: int = 0,
    ) -> None:
        self._ib = ib
        self._notifiers = notifiers
        self._db = db
        self._debounce_s = debounce_ms / 1000.0

        # Debounce state (only used when debounce_ms > 0)
        # _pending: orderId → {execId → Fill} — dict prevents duplicates
        self._pending: dict[str, dict[str, Fill]] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}

    _PRUNE_INTERVAL = 86400  # 24 hours

    def start(self) -> None:
        """Subscribe to execution and commission report events."""
        self._ib.execDetailsEvent += self._on_exec_details
        self._ib.commissionReportEvent += self._on_commission_report
        mode = f"debounce={self._debounce_s}s" if self._debounce_s > 0 else "immediate"
        log.info("Listener subscribed to execDetailsEvent + commissionReportEvent (%s)", mode)

        # Prune old dedup entries at startup, then daily
        self._prune()
        self._schedule_prune()

    def _prune(self) -> None:
        """Delete dedup entries older than 30 days."""
        try:
            prune(self._db, days=30)
        except Exception:
            log.exception("Dedup prune failed")

    def _schedule_prune(self) -> None:
        """Schedule the next daily prune."""
        loop = asyncio.get_event_loop()
        loop.call_later(self._PRUNE_INTERVAL, self._run_scheduled_prune)

    def _run_scheduled_prune(self) -> None:
        """Run prune and reschedule."""
        self._prune()
        self._schedule_prune()

    # ── execDetailsEvent (always immediate, no dedup) ────────────────────

    def _on_exec_details(self, trade: IBTrade, fill: IBFill) -> None:
        try:
            mapped = _map_to_fill(trade, fill, "execDetailsEvent")
        except Exception:
            log.exception(
                "Failed to map execDetailsEvent (execId=%s, orderId=%s)",
                fill.execution.execId, trade.order.permId,
            )
            return
        log.info(
            "execDetailsEvent: %s %s %s @ %.4f (execId=%s)",
            mapped.side.value, mapped.volume, mapped.symbol,
            mapped.price, mapped.execId,
        )
        self._dispatch(_fill_to_trade(mapped))

    # ── commissionReportEvent ────────────────────────────────────────────

    def _on_commission_report(
        self, trade: IBTrade, fill: IBFill, report: CommissionReport,
    ) -> None:
        try:
            mapped = _map_to_fill(trade, fill, "commissionReportEvent")
        except Exception:
            log.exception(
                "Failed to map commissionReportEvent (execId=%s, orderId=%s)",
                fill.execution.execId, trade.order.permId,
            )
            return

        if self._debounce_s > 0:
            self._enqueue(mapped)
        else:
            self._dispatch_immediate(mapped)

    def _dispatch_immediate(self, mapped: Fill) -> None:
        """Legacy path: dedup + dispatch a single fill immediately."""
        if is_processed(self._db, mapped.execId):
            log.info(
                "commissionReportEvent skipped (duplicate): %s %s %s (execId=%s)",
                mapped.side.value, mapped.volume, mapped.symbol, mapped.execId,
            )
            return

        log.info(
            "commissionReportEvent: %s %s %s fee=%.4f (execId=%s)",
            mapped.side.value, mapped.volume, mapped.symbol,
            mapped.fee, mapped.execId,
        )
        self._dispatch(_fill_to_trade(mapped), exec_ids=[mapped.execId])

    # ── Debounce logic ───────────────────────────────────────────────────

    def _enqueue(self, fill: Fill) -> None:
        """Buffer a fill and (re)start the debounce timer for its orderId."""
        order_id = fill.orderId
        log.info(
            "commissionReportEvent buffered: %s %s %s (execId=%s, orderId=%s)",
            fill.side.value, fill.volume, fill.symbol,
            fill.execId, order_id,
        )
        self._pending.setdefault(order_id, {})[fill.execId] = fill

        # Cancel existing timer and start a new one
        existing = self._timers.pop(order_id, None)
        if existing is not None:
            existing.cancel()

        loop = asyncio.get_event_loop()
        handle = loop.call_later(self._debounce_s, self._flush, order_id)
        self._timers[order_id] = handle

    def _flush(self, order_id: str) -> None:
        """Flush buffered fills for an orderId: dedup → aggregate → dispatch."""
        pending = self._pending.pop(order_id, {})
        self._timers.pop(order_id, None)

        fills = list(pending.values())
        if not fills:
            return

        # Filter against shared dedup DB
        all_ids = {f.execId for f in fills}
        already_seen = get_processed_ids(self._db, all_ids)
        new_fills = [f for f in fills if f.execId not in already_seen]

        if not new_fills:
            log.info(
                "Debounce flush: all %d fill(s) for orderId=%s already processed, skipping",
                len(fills), order_id,
            )
            return

        # Aggregate new fills into a single Trade
        trades = aggregate_fills(new_fills)
        if not trades:
            return

        trade = trades[0]  # All fills share the same orderId → 1 trade
        log.info(
            "Debounce flush: %s %s orderId=%s — %d new fill(s), vol=%.4f, avgPrice=%.4f",
            trade.side.value, trade.symbol, order_id,
            trade.fillCount, trade.volume, trade.price,
        )

        self._dispatch(trade, exec_ids=[f.execId for f in new_fills])

    # ── Dispatch ─────────────────────────────────────────────────────────

    def _dispatch(self, trade: Trade, *, exec_ids: list[str] | None = None) -> None:
        """Fire webhook in a background thread, then mark fills as processed.

        notify() runs via ``to_thread`` (blocking HTTP), but
        ``mark_processed_batch`` runs on the event-loop thread so the
        shared ``self._db`` connection is never touched from multiple
        threads concurrently.
        """
        payload = WebhookPayload(trades=[trade], errors=[])

        async def _send_and_mark() -> None:
            await asyncio.to_thread(notify, self._notifiers, payload)
            if exec_ids:
                mark_processed_batch(self._db, exec_ids)

        task = asyncio.create_task(_send_and_mark())
        task.add_done_callback(self._on_dispatch_done)

    @staticmethod
    def _on_dispatch_done(task: asyncio.Task[None]) -> None:
        """Log errors from background webhook dispatch."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.exception("Webhook dispatch failed", exc_info=exc)
