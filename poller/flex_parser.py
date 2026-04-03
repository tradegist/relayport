"""IBKR Flex XML parser — extracts fills and aggregates into trades."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any

from models import Fill, Trade

log = logging.getLogger("flex_parser")

# ── XML attribute → canonical Fill field name ────────────────────────────
# Only attributes whose XML name differs from the canonical model field.
# Attributes with the same name (e.g. "symbol", "currency") map directly.
_ATTR_ALIASES: dict[str, str] = {
    # Activity Flex <Trade>
    "ibCommission": "commission",
    "ibCommissionCurrency": "commissionCurrency",
    "ibOrderID": "orderId",
    "tradePrice": "price",
    "ibExecID": "ibExecId",
    "transactionID": "transactionId",
    # Trade Confirmation <TradeConfirm> / <TradeConfirmation>
    "orderID": "orderId",
    "execID": "ibExecId",
    "tax": "taxes",
    "settleDate": "settleDateTarget",
    "amount": "tradeMoney",
}

# Fill fields that are parsed as float (needed for aggregation).
_FLOAT_FIELDS: frozenset[str] = frozenset({
    "fxRateToBase", "quantity", "price", "taxes", "commission",
    "cost", "fifoPnlRealized", "tradeMoney", "proceeds", "netCash",
    "closePrice", "mtmPnl", "accruedInt",
})

# All known canonical field names on Fill.
_KNOWN_FIELDS: frozenset[str] = frozenset(Fill.model_fields.keys())

# XML tags that represent individual fills.
_FILL_TAGS: tuple[str, ...] = ("TradeConfirmation", "TradeConfirm", "Trade")


# ── Helpers ──────────────────────────────────────────────────────────────

def _parse_float(value: str, field: str, errors: list[str]) -> float:
    """Safely parse a string to float, appending to *errors* on failure."""
    if not value:
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        errors.append(f"Bad float for '{field}': {value!r}")
        return 0.0


def _dedup_id(fill: Fill) -> str:
    """Return the best available unique ID for dedup (transactionId preferred)."""
    return fill.transactionId or fill.ibExecId or fill.tradeID


# ── Parse ────────────────────────────────────────────────────────────────

def parse_fills(xml_text: str) -> tuple[list[Fill], list[str]]:
    """Parse Flex XML into individual Fill objects.

    Returns ``(fills, errors)`` where *errors* contains warnings about
    unknown attributes and any per-row parse problems.  Parsing never
    raises — broken rows are skipped and reported in *errors*.
    """
    fills: list[Fill] = []
    errors: list[str] = []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        errors.append(f"Failed to parse Flex XML: {exc}")
        return fills, errors

    seen: set[str] = set()
    reported_unknown: set[str] = set()

    for tag in _FILL_TAGS:
        for el in root.iter(tag):
            row_errors: list[str] = []

            # Map XML attributes → canonical names
            raw: dict[str, Any] = {}
            for attr_name, attr_value in el.attrib.items():
                canonical = _ATTR_ALIASES.get(attr_name, attr_name)
                if canonical in _KNOWN_FIELDS:
                    raw[canonical] = attr_value
                elif attr_name not in reported_unknown:
                    reported_unknown.add(attr_name)

            # Parse float fields
            kwargs: dict[str, Any] = {}
            for field_name, value in raw.items():
                if field_name in _FLOAT_FIELDS:
                    kwargs[field_name] = _parse_float(value, field_name, row_errors)
                else:
                    kwargs[field_name] = value

            # Dedup within this XML document
            try:
                fill = Fill(**kwargs)
            except Exception as exc:
                errors.append(f"Failed to create Fill from <{tag}>: {exc}")
                continue

            did = _dedup_id(fill)
            if not did or did in seen:
                continue
            seen.add(did)

            if row_errors:
                errors.extend(row_errors)

            fills.append(fill)

    if reported_unknown:
        errors.insert(
            0, f"Unknown XML attributes (ignored): {', '.join(sorted(reported_unknown))}"
        )

    return fills, errors


# ── Aggregate ────────────────────────────────────────────────────────────

def aggregate_fills(fills: list[Fill]) -> list[Trade]:
    """Group fills by ``orderId`` and compute aggregated Trade objects.

    * ``quantity`` — sum of all fills.
    * ``price`` — quantity-weighted average.
    * Financial fields (commission, taxes, …) — summed.
    * ``dateTime`` — last fill's value (lexicographic max).
    * String fields — last fill's value.
    * ``execIds`` — ``transactionId`` (or best dedup ID) per fill.
    * ``fillCount`` — number of fills in the group.
    """
    groups: dict[str, list[Fill]] = {}
    for fill in fills:
        if not fill.orderId:
            continue
        groups.setdefault(fill.orderId, []).append(fill)

    trades: list[Trade] = []
    for order_id, order_fills in groups.items():
        # Weighted average price
        abs_total = sum(abs(f.quantity) for f in order_fills)
        avg_price = (
            sum(abs(f.quantity) * f.price for f in order_fills) / abs_total
            if abs_total else 0.0
        )

        # Sum financial fields
        total_quantity = sum(f.quantity for f in order_fills)
        total_commission = sum(f.commission for f in order_fills)
        total_taxes = sum(f.taxes for f in order_fills)
        total_cost = sum(f.cost for f in order_fills)
        total_trade_money = sum(f.tradeMoney for f in order_fills)
        total_proceeds = sum(f.proceeds for f in order_fills)
        total_net_cash = sum(f.netCash for f in order_fills)
        total_fifo = sum(f.fifoPnlRealized for f in order_fills)
        total_mtm = sum(f.mtmPnl for f in order_fills)
        total_accrued = sum(f.accruedInt for f in order_fills)

        last = order_fills[-1]
        last_dt = max(f.dateTime for f in order_fills) if order_fills else ""

        # Fields that are explicitly overridden below — exclude from the
        # generic dict comprehension to avoid "multiple values" TypeError.
        _OVERRIDE_FIELDS = {
            "quantity", "price", "commission", "taxes", "cost",
            "tradeMoney", "proceeds", "netCash", "fifoPnlRealized",
            "mtmPnl", "accruedInt", "dateTime", "tradeDate",
        }

        # Build Trade from last fill's values, overriding aggregated fields.
        # Explicit kwargs preserve type safety (model_dump() returns
        # dict[str, Any] which defeats mypy checking).
        trades.append(Trade(
            **{
                field: getattr(last, field)
                for field in Fill.model_fields
                if field not in _OVERRIDE_FIELDS
            },
            quantity=total_quantity,
            price=round(avg_price, 8),
            commission=round(total_commission, 4),
            taxes=round(total_taxes, 4),
            cost=round(total_cost, 4),
            tradeMoney=round(total_trade_money, 4),
            proceeds=round(total_proceeds, 4),
            netCash=round(total_net_cash, 4),
            fifoPnlRealized=round(total_fifo, 4),
            mtmPnl=round(total_mtm, 4),
            accruedInt=round(total_accrued, 4),
            dateTime=last_dt,
            tradeDate=max(f.tradeDate for f in order_fills),
            execIds=[_dedup_id(f) for f in order_fills],
            fillCount=len(order_fills),
        ))

    return trades
