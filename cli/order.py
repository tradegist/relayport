import argparse
import json

from cli import die, load_env, relay_api


def run(args: argparse.Namespace) -> None:
    load_env()

    qty: int = args.quantity
    symbol: str = args.symbol
    order_type: str = args.order_type.upper()
    limit_price: float | None = args.limit_price
    currency: str = args.currency or "USD"
    exchange: str = args.exchange or "SMART"
    tif: str = args.tif.upper()
    outside_rth: bool = args.outside_rth

    if order_type == "LMT" and limit_price is None:
        die("lmtPrice required for LMT orders")

    action = "SELL" if qty < 0 else "BUY"
    abs_qty = abs(qty)

    # Build payload using the same structure as PlaceOrderRequest
    contract = {
        "symbol": symbol,
        "secType": "STK",
        "exchange": exchange,
        "currency": currency,
    }

    order: dict[str, str | int | float | bool] = {
        "action": action,
        "totalQuantity": abs_qty,
        "orderType": order_type,
        "tif": tif,
        "outsideRth": outside_rth,
    }

    if order_type == "LMT":
        order["lmtPrice"] = limit_price  # validated non-None above

    payload = {"contract": contract, "order": order}

    price_str = f" @ ${limit_price}" if limit_price else ""
    flags = []
    if tif != "DAY":
        flags.append(tif)
    if outside_rth:
        flags.append("outsideRth")
    flags_str = f" [{', '.join(flags)}]" if flags else ""

    print(f"Placing order: {action} {abs_qty} {symbol} {order_type}"
          f"{price_str} ({currency}/{exchange}){flags_str}")

    data = relay_api("/ibkr/order", data=payload)
    print(json.dumps(data, indent=4))
