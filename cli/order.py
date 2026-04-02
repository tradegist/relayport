import json

from cli import load_env, relay_api, die


def run(args):
    load_env()

    qty = args.quantity
    symbol = args.symbol
    order_type = args.order_type.upper()

    if order_type == "LMT" and args.limit_price is None:
        die("limitPrice required for LMT orders")

    payload = {
        "quantity": qty,
        "symbol": symbol,
        "orderType": order_type,
    }

    if order_type == "LMT":
        payload["limitPrice"] = args.limit_price

    currency = args.currency or "USD"
    exchange = args.exchange or "SMART"

    if currency != "USD":
        payload["currency"] = currency
    if exchange != "SMART":
        payload["exchange"] = exchange

    action = "SELL" if qty < 0 else "BUY"
    abs_qty = abs(qty)
    price_str = f" @ ${args.limit_price}" if args.limit_price else ""

    print(f"Placing order: {action} {abs_qty} {symbol} {order_type}"
          f"{price_str} ({currency}/{exchange})")

    data = relay_api("/ibkr/order", data=payload)
    print(json.dumps(data, indent=4))
