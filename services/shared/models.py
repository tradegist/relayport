"""Shared Pydantic models and type aliases (CommonFill contract).

!! PUBLIC CONTRACT — every type defined here is exported to consumers
!! via the generated TypeScript and Python type packages (make types).
!! Do NOT add general internal helpers, mapping dicts, or utility
!! functions here — those belong in utilities.py.

Outbound webhook payload contracts live in relay_core/notifier/models.py.
"""

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

RelayName = Literal["ibkr", "kraken"]
"""Allowed relay identifiers — single source of truth.

Used to validate RELAYS env var, discriminate webhook payloads, and
namespace dedup/metadata DB keys. Add new brokers here.
"""

AssetClass = Literal["equity", "option", "crypto", "future", "forex", "other"]

OrderType = Literal["market", "limit", "stop", "stop_limit", "trailing_stop"]


class BuySell(str, Enum):
    BUY = "buy"
    SELL = "sell"


Source = Literal[
    "flex", "execDetailsEvent", "commissionReportEvent",  # IBKR
    "rest_poll", "ws_execution",                           # Kraken
]


class Fill(BaseModel):
    """Individual execution from a broker (CommonFill spec)."""

    model_config = ConfigDict(extra="forbid")

    execId: str
    orderId: str
    symbol: str
    assetClass: AssetClass
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
    assetClass: AssetClass
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
