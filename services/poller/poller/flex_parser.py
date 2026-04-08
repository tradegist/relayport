"""IBKR Flex XML parser — extracts fills and aggregates into trades.

The parser puts ALL XML attributes into Fill.raw as a flat dict (canonical
names, floats parsed).  CommonFill fields (execId, symbol, side, …) are
extracted from raw into top-level Fill fields.

Commonly-accessed raw keys (Flex XML source):
  Account:    accountId, acctAlias, model
  Currency:   currency, fxRateToBase, commissionCurrency
  Security:   assetCategory, conid, isin, figi, listingExchange, multiplier
  Trade IDs:  ibExecId, transactionId, tradeID, brokerageOrderID
  Order:      orderTime, orderType (raw string), orderReference
  Financial:  commission, taxes, tradeMoney, proceeds, netCash,
              fifoPnlRealized, mtmPnl, closePrice, accruedInt
  Dates:      dateTime, tradeDate, reportDate, settleDateTarget
  Position:   openCloseIndicator, notes, quantity (raw float)

The listener (ib_async source) populates a smaller raw dict:
  ibExecId, orderId, side, quantity, price, symbol, assetCategory,
  exchange, currency, commission, commissionCurrency, fifoPnlRealized,
  dateTime, accountId
"""

import logging
import xml.etree.ElementTree as ET
from typing import Any

from models_poller import BuySell, Fill
from shared import normalize_order_type

log = logging.getLogger("flex_parser")

# ── XML attribute → canonical name ───────────────────────────────────────
# Only attributes whose XML name differs from a useful canonical name.
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

# Canonical fields that should be parsed as float.
_FLOAT_FIELDS: frozenset[str] = frozenset({
    "fxRateToBase", "quantity", "price", "taxes", "commission",
    "cost", "fifoPnlRealized", "tradeMoney", "proceeds", "netCash",
    "closePrice", "mtmPnl", "accruedInt",
})

# XML tags that represent individual fills.
_FILL_TAGS: tuple[str, ...] = ("TradeConfirmation", "TradeConfirm", "Trade")

# Flex XML buySell values → BuySell enum (lowercase).
_SIDE_MAP: dict[str, BuySell] = {
    "BUY": BuySell.BUY,
    "SELL": BuySell.SELL,
}


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

    for tag in _FILL_TAGS:
        for el in root.iter(tag):
            row_errors: list[str] = []

            # Map XML attributes → canonical names, parse floats
            raw: dict[str, Any] = {}
            for attr_name, attr_value in el.attrib.items():
                canonical = _ATTR_ALIASES.get(attr_name, attr_name)
                if canonical in _FLOAT_FIELDS:
                    raw[canonical] = _parse_float(attr_value, canonical, row_errors)
                else:
                    raw[canonical] = attr_value

            # Resolve execId (dedup key) via fallback chain
            exec_id = str(
                raw.get("ibExecId", "")
                or raw.get("transactionId", "")
                or raw.get("tradeID", "")
            )
            if not exec_id:
                errors.append(
                    f"Skipping <{tag}>: no execId (ibExecId, transactionId, tradeID all empty)"
                )
                continue

            # Map buySell to BuySell enum
            side_str = str(raw.get("buySell", ""))
            side = _SIDE_MAP.get(side_str)
            if side is None:
                errors.append(f"Failed to create Fill from <{tag}>: unknown buySell {side_str!r}")
                continue

            # Build CommonFill
            try:
                fill = Fill(
                    execId=exec_id,
                    orderId=str(raw.get("orderId", "")),
                    symbol=str(raw.get("symbol", "")),
                    side=side,
                    orderType=normalize_order_type(str(raw.get("orderType", ""))),
                    price=float(raw.get("price", 0.0)),
                    volume=float(raw.get("quantity", 0.0)),
                    cost=float(raw.get("cost", 0.0)),
                    fee=float(raw.get("commission", 0.0)),
                    timestamp=str(raw.get("dateTime", "")),
                    source="flex",
                    raw=raw,
                )
            except Exception as exc:
                errors.append(f"Failed to create Fill from <{tag}>: {exc}")
                continue

            # Dedup within this XML document
            if fill.execId in seen:
                continue
            seen.add(fill.execId)

            if row_errors:
                errors.extend(row_errors)

            fills.append(fill)

    return fills, errors
