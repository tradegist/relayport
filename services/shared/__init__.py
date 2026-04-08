"""Shared models and utilities used by both poller and remote-client."""

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

OrderType = Literal["market", "limit", "stop", "stop_limit", "trailing_stop"]

# IBKR order type strings → normalized OrderType.
# Used by the Flex parser; the listener doesn't receive order type info.
_ORDER_TYPE_MAP: dict[str, OrderType] = {
    "MKT": "market",
    "LMT": "limit",
    "STP": "stop",
    "STP LMT": "stop_limit",
    "TRAIL": "trailing_stop",
    "TRAIL LMT": "trailing_stop",
    "TRAIL LIMIT": "trailing_stop",
}


def normalize_order_type(raw: str) -> OrderType | None:
    """Map an IBKR order type string to the normalized OrderType literal.

    Returns None when the raw value is not in the known mapping.
    """
    return _ORDER_TYPE_MAP.get(raw)


class BuySell(str, Enum):
    BUY = "buy"
    SELL = "sell"


Source = Literal["flex", "execDetailsEvent", "commissionReportEvent"]


class Fill(BaseModel):
    """Individual execution from IBKR (CommonFill spec)."""

    model_config = ConfigDict(extra="forbid")

    execId: str
    orderId: str
    symbol: str
    side: BuySell
    orderType: OrderType | None = None
    price: float
    volume: float
    cost: float
    fee: float
    timestamp: str
    source: Source
    raw: dict[str, Any]


class Trade(BaseModel):
    """Aggregated trade (one or more fills for the same order)."""

    model_config = ConfigDict(extra="forbid")

    orderId: str
    symbol: str
    side: BuySell
    orderType: OrderType | None = None
    price: float
    volume: float
    cost: float
    fee: float
    fillCount: int
    execIds: list[str]
    timestamp: str
    source: Source
    raw: dict[str, Any]


class WebhookPayload(BaseModel):
    """Payload sent to the target webhook URL."""

    model_config = ConfigDict(extra="forbid")

    trades: list[Trade]
    errors: list[str]


def _dedup_id(fill: Fill) -> str:
    """Return the dedup key for a fill.

    The parser must resolve ``execId`` at construction time (e.g. IBKR
    uses ibExecId → transactionId → tradeID fallback). After that,
    ``execId`` is the canonical dedup key.
    """
    return fill.execId


def aggregate_fills(fills: list[Fill]) -> list[Trade]:
    """Group fills by ``orderId`` and compute aggregated Trade objects.

    * ``volume`` — sum of all fills.
    * ``price`` — quantity-weighted average (VWAP).
    * Financial fields (cost, fee) — summed.
    * ``timestamp`` — latest fill's value (lexicographic max).
    * ``execIds`` — execId per fill.
    * ``fillCount`` — number of fills in the group.
    * ``raw`` — first fill's raw dict.
    """
    groups: dict[str, list[Fill]] = {}
    for fill in fills:
        if not fill.orderId:
            continue
        groups.setdefault(fill.orderId, []).append(fill)

    trades: list[Trade] = []
    for _order_id, order_fills in groups.items():
        # Weighted average price
        abs_total = sum(abs(f.volume) for f in order_fills)
        avg_price = (
            sum(abs(f.volume) * f.price for f in order_fills) / abs_total
            if abs_total else 0.0
        )

        total_volume = sum(f.volume for f in order_fills)
        total_cost = sum(f.cost for f in order_fills)
        total_fee = sum(f.fee for f in order_fills)

        last = order_fills[-1]
        last_ts = max(f.timestamp for f in order_fills) if order_fills else ""

        trades.append(Trade(
            orderId=last.orderId,
            symbol=last.symbol,
            side=last.side,
            orderType=last.orderType,
            price=round(avg_price, 8),
            volume=total_volume,
            cost=round(total_cost, 4),
            fee=round(total_fee, 4),
            fillCount=len(order_fills),
            execIds=[f.execId for f in order_fills],
            timestamp=last_ts,
            source=last.source,
            raw=order_fills[0].raw,
        ))

    return trades


# ── Schema export (used by schema_gen.py → make types) ──────────────

SCHEMA_MODELS: list[type[BaseModel]] = [WebhookPayload, Trade, Fill]
