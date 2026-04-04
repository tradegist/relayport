"""Shared Pydantic models — single source of truth for webhook payload types.

Fill  = individual execution from IBKR Flex XML (all known fields).
Trade = one or more fills aggregated by orderId.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class BuySell(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Fill(BaseModel):
    """Individual execution / fill from IBKR Flex XML."""

    model_config = ConfigDict(extra="forbid")

    # ── Account ──────────────────────────────────────────────────────────
    accountId: str = ""
    acctAlias: str = ""
    model: str = ""

    # ── Currency ─────────────────────────────────────────────────────────
    currency: str = ""
    fxRateToBase: float = 0.0

    # ── Security identification ──────────────────────────────────────────
    assetCategory: str = ""
    subCategory: str = ""
    symbol: str = ""
    description: str = ""
    conid: str = ""
    securityID: str = ""
    securityIDType: str = ""
    cusip: str = ""
    isin: str = ""
    figi: str = ""
    listingExchange: str = ""
    multiplier: str = ""

    # ── Underlying (derivatives) ─────────────────────────────────────────
    underlyingConid: str = ""
    underlyingSymbol: str = ""
    underlyingSecurityID: str = ""
    underlyingListingExchange: str = ""

    # ── Issuer ───────────────────────────────────────────────────────────
    issuer: str = ""
    issuerCountryCode: str = ""

    # ── Options / derivatives ────────────────────────────────────────────
    strike: str = ""
    expiry: str = ""
    putCall: str = ""

    # ── Trade IDs ────────────────────────────────────────────────────────
    tradeID: str = ""
    transactionId: str = ""       # AF: transactionID
    ibExecId: str = ""            # AF: ibExecID, TC: execID
    brokerageOrderID: str = ""
    exchOrderId: str = ""
    extExecID: str = ""

    # ── Order ────────────────────────────────────────────────────────────
    orderId: str = ""             # AF: ibOrderID, TC: orderID
    orderTime: str = ""
    orderType: str = ""
    orderReference: str = ""

    # ── Trade details ────────────────────────────────────────────────────
    transactionType: str = ""
    exchange: str = ""
    buySell: BuySell
    quantity: float = 0.0
    price: float = 0.0            # AF: tradePrice, TC: price

    # ── Financial ────────────────────────────────────────────────────────
    taxes: float = 0.0
    commission: float = 0.0       # AF: ibCommission, TC: commission
    commissionCurrency: str = ""  # AF: ibCommissionCurrency, TC: commissionCurrency
    cost: float = 0.0
    fifoPnlRealized: float = 0.0
    tradeMoney: float = 0.0
    proceeds: float = 0.0
    netCash: float = 0.0
    closePrice: float = 0.0
    mtmPnl: float = 0.0
    accruedInt: float = 0.0

    # ── Dates ────────────────────────────────────────────────────────────
    dateTime: str = ""
    tradeDate: str = ""
    reportDate: str = ""
    settleDateTarget: str = ""

    # ── Position ─────────────────────────────────────────────────────────
    openCloseIndicator: str = ""
    notes: str = ""

    # ── Original trade (corrections) ─────────────────────────────────────
    origTradePrice: str = ""
    origTradeDate: str = ""
    origTradeID: str = ""
    origOrderID: str = ""
    origTransactionID: str = ""

    # ── Clearing / related ───────────────────────────────────────────────
    clearingFirmID: str = ""
    relatedTradeID: str = ""
    relatedTransactionID: str = ""
    rtn: str = ""
    volatilityOrderLink: str = ""

    # ── Timing ───────────────────────────────────────────────────────────
    openDateTime: str = ""
    holdingPeriodDateTime: str = ""
    whenRealized: str = ""
    whenReopened: str = ""

    # ── Metadata ─────────────────────────────────────────────────────────
    levelOfDetail: str = ""
    changeInPrice: str = ""
    changeInQuantity: str = ""
    traderID: str = ""
    isAPIOrder: str = ""
    principalAdjustFactor: str = ""
    initialInvestment: str = ""
    positionActionID: str = ""
    serialNumber: str = ""
    deliveryType: str = ""
    commodityType: str = ""
    fineness: str = ""
    weight: str = ""


class Trade(Fill):
    """Aggregated trade — one or more fills grouped by orderId.

    Numeric fields (quantity, price, commission, taxes, …) are aggregated.
    String fields use the last fill's value.
    ``price`` is the quantity-weighted average across fills.
    """

    execIds: list[str] = Field(default_factory=list)
    fillCount: int = 0


class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trades: list[Trade]
    errors: list[str]


if __name__ == "__main__":
    import json
    import sys

    schema = WebhookPayload.model_json_schema()

    # Strip per-property "title" keys so json-schema-to-typescript
    # inlines primitive types (string, number) instead of emitting
    # a named type alias for every single field.
    def _strip_titles(obj: object) -> None:
        if isinstance(obj, dict):
            for key, val in list(obj.items()):
                if key == "properties" and isinstance(val, dict):
                    for prop in val.values():
                        if isinstance(prop, dict):
                            prop.pop("title", None)
                _strip_titles(val)
        elif isinstance(obj, list):
            for item in obj:
                _strip_titles(item)

    _strip_titles(schema)

    json.dump(schema, sys.stdout, indent=2)
    sys.stdout.write("\n")
