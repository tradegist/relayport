"""WebSocket event types — copied from ibkr-bridge-types.

These models mirror ``ibkr_bridge_types.models`` (generated from
``services/bridge/bridge_models.py`` in the ibkr_bridge project).
Copied inline to avoid a cross-project dependency.
When ibkr-bridge-types is published to PyPI, replace this file with
a pip dependency.

!! Do not edit manually — re-copy from ibkr_bridge when models change.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

WsEventType = Literal[
    "execDetailsEvent",
    "commissionReportEvent",
    "connected",
    "disconnected",
]


class WsComboLeg(BaseModel):
    """Mirrors ib_async.contract.ComboLeg (ib_async 2.1.0)."""

    model_config = ConfigDict(extra="forbid")

    conId: int
    ratio: int
    action: str
    exchange: str
    openClose: int
    shortSaleSlot: int
    designatedLocation: str
    exemptCode: int


class WsDeltaNeutralContract(BaseModel):
    """Mirrors ib_async.contract.DeltaNeutralContract (ib_async 2.1.0)."""

    model_config = ConfigDict(extra="forbid")

    conId: int
    delta: float
    price: float


class WsContract(BaseModel):
    """Mirrors ib_async.contract.Contract (ib_async 2.1.0)."""

    model_config = ConfigDict(extra="forbid")

    secType: str
    conId: int
    symbol: str
    lastTradeDateOrContractMonth: str
    strike: float
    right: str
    multiplier: str
    exchange: str
    primaryExchange: str
    currency: str
    localSymbol: str
    tradingClass: str
    includeExpired: bool
    secIdType: str
    secId: str
    description: str
    issuerId: str
    comboLegsDescrip: str
    comboLegs: list[WsComboLeg] = Field(default_factory=list)
    deltaNeutralContract: WsDeltaNeutralContract | None = None


class WsExecution(BaseModel):
    """Mirrors ib_async.objects.Execution (ib_async 2.1.0)."""

    model_config = ConfigDict(extra="forbid")

    execId: str
    time: str
    acctNumber: str
    exchange: str
    side: str
    shares: float
    price: float
    permId: int
    clientId: int
    orderId: int
    liquidation: int
    cumQty: float
    avgPrice: float
    orderRef: str
    evRule: str
    evMultiplier: float
    modelCode: str
    lastLiquidity: int
    pendingPriceRevision: bool


class WsCommissionReport(BaseModel):
    """Mirrors ib_async.objects.CommissionReport (ib_async 2.1.0)."""

    model_config = ConfigDict(extra="forbid")

    execId: str
    commission: float
    currency: str
    realizedPNL: float
    yield_: float
    yieldRedemptionDate: int


class WsFill(BaseModel):
    """Mirrors ib_async.objects.Fill NamedTuple (ib_async 2.1.0)."""

    model_config = ConfigDict(extra="forbid")

    contract: WsContract
    execution: WsExecution
    commissionReport: WsCommissionReport
    time: str


class WsEnvelope(BaseModel):
    """Top-level WebSocket message wrapper."""

    model_config = ConfigDict(extra="forbid")

    type: WsEventType
    seq: int
    timestamp: str
    fill: WsFill | None = None
