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


# ── Request models ───────────────────────────────────────────────────

class ContractRequest(BaseModel):
    """Contract fields for identifying the instrument (mirrors ib_async.Contract)."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    secType: SecType = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    primaryExchange: str = ""


class OrderRequest(BaseModel):
    """Order fields for specifying the trade (mirrors ib_async.Order)."""

    model_config = ConfigDict(extra="forbid")

    action: Action
    totalQuantity: float = Field(gt=0)
    orderType: OrderType
    lmtPrice: float | None = None
    tif: TimeInForce = "DAY"
    outsideRth: bool = False


class PlaceOrderRequest(BaseModel):
    """Top-level request body for POST /ibkr/order."""

    model_config = ConfigDict(extra="forbid")

    contract: ContractRequest
    order: OrderRequest


# ── Response models ──────────────────────────────────────────────────

class OrderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    orderId: int
    action: Action
    symbol: str
    totalQuantity: float
    orderType: OrderType
    lmtPrice: float | None = None


if __name__ == "__main__":
    import json
    import sys

    # Generate a combined schema with all types reachable from the root.
    # json-schema-to-typescript only emits types it can reach, so we use
    # anyOf to reference both PlaceOrderRequest and OrderResponse.
    req_schema = PlaceOrderRequest.model_json_schema()
    resp_schema = OrderResponse.model_json_schema()

    defs = req_schema.get("$defs", {})
    defs.update(resp_schema.get("$defs", {}))

    # Move both models into $defs
    defs["PlaceOrderRequest"] = {
        k: v for k, v in req_schema.items() if k != "$defs"
    }
    defs["OrderResponse"] = {
        k: v for k, v in resp_schema.items() if k != "$defs"
    }

    schema = {
        "$defs": defs,
        "anyOf": [
            {"$ref": "#/$defs/PlaceOrderRequest"},
            {"$ref": "#/$defs/OrderResponse"},
        ],
    }

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
