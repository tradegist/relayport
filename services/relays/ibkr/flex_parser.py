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

References (IBKR official field dictionaries — authoritative source for
what attributes Flex XML may contain):
  Activity Flex Query Reference:
    https://www.ibkrguides.com/reportingreference/reportguide/activity%20flex%20query%20reference.htm
  Trade Confirmation Flex Query Reference:
    https://www.ibkrguides.com/reportingreference/reportguide/trade%20confirmation%20flex%20query%20reference.htm
  Trades section (Activity Flex):
    https://www.ibkrguides.com/reportingreference/reportguide/tradesfq.htm

Answer to "is there an exchange-rate field?": Flex uses `fxRateToBase`
(asset currency → base currency), not `exchangeRate`.
"""

import logging
import xml.etree.ElementTree as ET
from datetime import tzinfo
from typing import Any, Literal

from shared import BuySell, Fill, OptionContract, normalize_timestamp

from .timestamps import flex_date_to_iso, flex_to_iso
from .utilities import normalize_asset_class, normalize_order_type

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
    "closePrice", "mtmPnl", "accruedInt", "strike",
})

# Flex ``putCall`` values → OptionContract.type literals.
_OPT_TYPE_MAP: dict[str, Literal["call", "put"]] = {
    "C": "call",
    "P": "put",
}

# XML tags that represent individual fills.
_FILL_TAGS: tuple[str, ...] = ("TradeConfirmation", "TradeConfirm", "Trade")


def _validate_fill_tags(tags: tuple[str, ...]) -> None:
    """Reject ``<Order>`` summary elements as a fill source.

    Activity Flex emits an ``<Order levelOfDetail="ORDER" ...>`` summary
    immediately before its execution rows. Treating it as a fill would
    double-count every order — it has no ``ibExecID``/``transactionID``
    so the dedup guard wouldn't catch it (the parser would fall back to
    ``tradeID``, which is also empty on Order rows, and skip it… but if
    IBKR ever populates ``tradeID`` on Order rows, every executed order
    would silently produce a duplicate Fill).
    """
    if "Order" in tags:
        raise RuntimeError(
            "_FILL_TAGS must not include 'Order' — Order rows are summary "
            "elements (levelOfDetail=ORDER), not executions."
        )


_validate_fill_tags(_FILL_TAGS)

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


def _build_option_contract(
    raw: dict[str, Any], tag: str, exec_id: str, errors: list[str],
) -> OptionContract | None:
    """Assemble an :class:`OptionContract` from a Flex option row's raw attrs.

    Returns ``None`` and appends a per-row reason to *errors* when any
    required field is missing or malformed. Caller skips the row in that
    case — see comment at the call site for the rationale.
    """
    root_symbol = str(raw.get("underlyingSymbol", "")).strip()
    if not root_symbol:
        errors.append(
            f"Skipping option <{tag}> execId={exec_id}: empty underlyingSymbol"
        )
        return None

    # ``strike`` is in _FLOAT_FIELDS so raw["strike"] is already a float
    # (defaulting to 0.0 when the attr was missing/empty). A real option
    # always has a positive strike.
    strike = float(raw.get("strike", 0.0))
    if strike <= 0:
        errors.append(
            f"Skipping option <{tag}> execId={exec_id}: non-positive strike {strike!r}"
        )
        return None

    expiry_raw = str(raw.get("expiry", "")).strip()
    if not expiry_raw:
        errors.append(
            f"Skipping option <{tag}> execId={exec_id}: empty expiry"
        )
        return None
    try:
        expiry_iso = flex_date_to_iso(expiry_raw)
    except ValueError as exc:
        errors.append(
            f"Skipping option <{tag}> execId={exec_id}: bad expiry {expiry_raw!r}: {exc}"
        )
        return None

    put_call = str(raw.get("putCall", "")).strip()
    opt_type = _OPT_TYPE_MAP.get(put_call)
    if opt_type is None:
        errors.append(
            f"Skipping option <{tag}> execId={exec_id}: unknown putCall {put_call!r}"
        )
        return None

    return OptionContract(
        rootSymbol=root_symbol,
        strike=strike,
        expiryDate=expiry_iso,
        type=opt_type,
    )


# ── Parse ────────────────────────────────────────────────────────────────

def parse_fills(
    xml_text: str, *, tz: tzinfo | None = None,
) -> tuple[list[Fill], list[str]]:
    """Parse Flex XML into individual Fill objects.

    *tz* is the IANA timezone to interpret IBKR's naive ``dateTime``
    values in (typically the account base tz from ``IBKR_ACCOUNT_TIMEZONE``).
    Defaults to UTC when omitted.

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

            # Map assetCategory to AssetClass
            asset_raw = str(raw.get("assetCategory", ""))
            asset_class = normalize_asset_class(asset_raw)
            if asset_class == "other":
                row_errors.append(f"Unknown assetCategory {asset_raw!r}, using 'other'")

            # Build CommonFill
            currency_raw = str(raw.get("currency", "")).strip().upper()
            currency = currency_raw or None

            # OptionContract — only built for option rows. Skip the row if
            # any required option field is missing or invalid: emitting an
            # option fill with incomplete metadata produces a webhook payload
            # that consumers can't reliably interpret (e.g. unknown strike or
            # type), worse than missing the fill altogether.
            option: OptionContract | None = None
            if asset_class == "option":
                option = _build_option_contract(raw, tag, exec_id, errors)
                if option is None:
                    continue

            ts_raw = str(raw.get("dateTime", ""))
            if ts_raw:
                try:
                    ts = normalize_timestamp(flex_to_iso(ts_raw), assume_tz=tz)
                except ValueError as exc:
                    errors.append(
                        f"Skipping <{tag}> execId={exec_id}: bad dateTime {ts_raw!r}: {exc}"
                    )
                    continue
            else:
                # Minimal test fixtures sometimes omit dateTime. Pass through
                # as empty so the fill is still parseable (the watermark and
                # FX enrichment simply skip fills without a usable timestamp).
                ts = ""

            try:
                fill = Fill(
                    execId=exec_id,
                    orderId=str(raw.get("orderId", "")),
                    symbol=str(raw.get("symbol", "")),
                    assetClass=asset_class,
                    side=side,
                    orderType=normalize_order_type(str(raw.get("orderType", ""))),
                    price=float(raw.get("price", 0.0)),
                    volume=float(raw.get("quantity", 0.0)),
                    cost=float(raw.get("cost", 0.0)),
                    fee=abs(float(raw.get("commission", 0.0))),
                    timestamp=ts,
                    source="flex",
                    currency=currency,
                    option=option,
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
