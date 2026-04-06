"""Pydantic models for the remote-client REST API (order placement)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ── Strict union types aligned with ib_async ─────────────────────────

Action = Literal["BUY", "SELL"]

OrderType = Literal["MKT", "LMT"]

SecType = Literal[
    "STK", "OPT", "FUT", "IND", "FOP", "CASH",
    "CFD", "BAG", "WAR", "BOND", "CMDTY", "NEWS",
    "FUND", "CRYPTO", "EVENT",
]

TimeInForce = Literal["DAY", "GTC", "IOC", "GTD", "OPG", "FOK", "DTC"]


# ── POST /ibkr/order ─────────────────────────────────────────────────

class ContractPayload(BaseModel):
    """Contract fields for identifying the instrument (mirrors ib_async.Contract)."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    secType: SecType = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    primaryExchange: str = ""


class OrderPayload(BaseModel):
    """Order fields for specifying the trade (mirrors ib_async.Order)."""

    model_config = ConfigDict(extra="forbid")

    action: Action
    totalQuantity: float = Field(gt=0)
    orderType: OrderType
    lmtPrice: float | None = None
    tif: TimeInForce = "DAY"
    outsideRth: bool = False


class PlaceOrderPayload(BaseModel):
    """Top-level request body for POST /ibkr/order."""

    model_config = ConfigDict(extra="forbid")

    contract: ContractPayload
    order: OrderPayload


class PlaceOrderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    orderId: int  # Permanent order ID (permId from ib_async)
    action: Action
    symbol: str
    totalQuantity: float
    orderType: OrderType
    lmtPrice: float | None = None


# ── GET /health ──────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connected: bool
    tradingMode: str


# ── GET /ibkr/trades ─────────────────────────────────────────────────

class FillDetail(BaseModel):
    """Single execution fill within a trade."""

    model_config = ConfigDict(extra="forbid")

    execId: str
    time: str
    exchange: str
    side: str
    shares: float
    price: float
    commission: float
    commissionCurrency: str
    realizedPNL: float


class TradeDetail(BaseModel):
    """A trade with its order info, status, and fills."""

    model_config = ConfigDict(extra="forbid")

    # Order identification
    orderId: int  # Permanent order ID (permId from ib_async)
    action: str  # str not Action — IB may return values beyond BUY/SELL for reads
    totalQuantity: float
    orderType: str  # str not OrderType — IB returns STP, TRAIL, etc. for existing orders
    lmtPrice: float | None = None
    tif: TimeInForce

    # Contract
    symbol: str
    secType: SecType
    exchange: str
    currency: str

    # Status
    status: str
    filled: float
    remaining: float
    avgFillPrice: float

    # Fills
    fills: list[FillDetail]


class ListTradesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trades: list[TradeDetail]


# ── Schema export (used by schema_gen.py → make types) ──────────────

SCHEMA_MODELS: list[type[BaseModel]] = [
    PlaceOrderPayload,
    PlaceOrderResponse,
    HealthResponse,
    ListTradesResponse,
]
