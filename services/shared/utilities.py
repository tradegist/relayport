"""Internal utilities for mapping IBKR values and aggregating fills.

These are internal to the relay — not exported to consumer packages.
"""

from .models import AssetClass, Fill, OrderType, Trade

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


# IBKR asset category / secType → normalized AssetClass.
# Used by both the Flex parser (assetCategory attr) and the listener (contract.secType).
_ASSET_CLASS_MAP: dict[str, AssetClass] = {
    "STK": "equity",
    "OPT": "option",
    "FUT": "future",
    "CRYPTO": "crypto",
    "CASH": "forex",
}


def normalize_asset_class(raw: str) -> AssetClass:
    """Map an IBKR asset category string to the normalized AssetClass literal.

    Returns ``"other"`` when the raw value is not in the known mapping.
    """
    return _ASSET_CLASS_MAP.get(raw, "other")


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
            assetClass=last.assetClass,
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
