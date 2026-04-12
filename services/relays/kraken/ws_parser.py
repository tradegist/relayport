"""Parse Kraken WebSocket v2 execution messages into Fill models."""

from __future__ import annotations

from relay_core.parsing import require_float, require_str
from shared import BuySell, Fill, OrderType

from .kraken_types import KrakenWsExecution, KrakenWsMessage

# Kraken order type strings -> normalized OrderType.
_ORDER_TYPE_MAP: dict[str, OrderType] = {
    "market": "market",
    "limit": "limit",
    "stop-loss": "stop",
    "stop-loss-limit": "stop_limit",
    "trailing-stop": "trailing_stop",
    "trailing-stop-limit": "trailing_stop",
}


def normalize_order_type(raw: str) -> OrderType | None:
    """Map a Kraken order type string to the normalized OrderType literal."""
    return _ORDER_TYPE_MAP.get(raw)


def parse_executions(msg: KrakenWsMessage) -> tuple[list[Fill], list[str]]:
    """Parse a WS v2 executions channel message.

    Returns (fills, errors) where fills are successfully parsed execution
    events with exec_type == 'trade', and errors are human-readable parse
    error strings (never raises).
    """
    fills: list[Fill] = []
    errors: list[str] = []

    channel = msg.get("channel")
    if channel != "executions":
        return fills, errors

    data = msg.get("data")
    if not isinstance(data, list):
        errors.append(f"executions message missing 'data' list: {list(msg.keys())}")
        return fills, errors

    for item in data:
        if not isinstance(item, dict):
            errors.append(f"executions data item is not a dict: {type(item).__name__}")
            continue

        exec_type = item.get("exec_type")
        if exec_type != "trade":
            continue

        try:
            fill = _parse_fill(item)
            fills.append(fill)
        except Exception as exc:
            exec_id = item.get("exec_id", "unknown")
            errors.append(f"Failed to parse fill exec_id={exec_id}: {exc}")

    return fills, errors


def _extract_fee(item: KrakenWsExecution) -> float:
    """Return the fee for a single execution event.

    Preference order:
    1. ``fee_usd_equiv`` — Kraken's pre-converted USD equivalent; always
       meaningful regardless of how many fee currencies are involved.
    2. Single-asset fallback — sum ``abs(qty)`` across entries only when all
       entries share the same asset (summing across different assets would
       produce a number in no real currency).
    3. Zero if neither applies.
    """
    fee_usd_equiv = item.get("fee_usd_equiv")
    if isinstance(fee_usd_equiv, (int, float)):
        return abs(float(fee_usd_equiv))

    fees = item.get("fees")
    if not isinstance(fees, list) or not fees:
        return 0.0

    assets = {entry["asset"] for entry in fees if isinstance(entry, dict) and "asset" in entry}
    if len(assets) != 1:
        # Mixed currencies — cannot produce a meaningful scalar; return 0.0.
        return 0.0

    return sum(
        abs(float(entry.get("qty", 0.0)))
        for entry in fees
        if isinstance(entry, dict)
    )


def _parse_fill(item: KrakenWsExecution) -> Fill:
    """Convert a single WS execution message to a Fill model."""
    ctx = f"WS exec {item.get('exec_id', 'unknown')}"
    total_fee = _extract_fee(item)

    side_raw = require_str(item, "side", ctx)
    if side_raw == "buy":
        side = BuySell.BUY
    elif side_raw == "sell":
        side = BuySell.SELL
    else:
        raise ValueError(f"{ctx}: invalid side {side_raw!r}")

    order_type = normalize_order_type(require_str(item, "order_type", ctx))

    return Fill(
        execId=require_str(item, "exec_id", ctx),
        orderId=require_str(item, "order_id", ctx),
        symbol=require_str(item, "symbol", ctx),
        assetClass="crypto",
        side=side,
        orderType=order_type,
        price=require_float(item, "last_price", ctx),
        volume=require_float(item, "last_qty", ctx),
        cost=require_float(item, "cost", ctx),
        fee=total_fee,
        timestamp=require_str(item, "timestamp", ctx),
        source="ws_execution",
        raw=dict(item),
    )
