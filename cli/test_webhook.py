"""Send a simulated webhook payload with 3 sample trades."""

import hashlib
import hmac
import urllib.request
import urllib.error

from cli import load_env, env, die
from models import Trade, WebhookPayload


SAMPLE_TRADES = [
    Trade(
        symbol="AAPL",
        underlyingSymbol="AAPL",
        assetCategory="STK",
        listingExchange="NASDAQ",
        buySell="BUY",
        quantity=10.0,
        price=187.5200,
        tradeDate="20260402",
        dateTime="20260402;143015",
        orderTime="20260402;143000",
        orderId="1001",
        transactionId="0000e001.fake0001.01.01",
        execIds=["0000e001.fake0001.01.01"],
        accountId="UXXXXXXX",
        commission=1.0,
        commissionCurrency="USD",
        currency="USD",
        orderType="MKT",
        fillCount=1,
    ),
    Trade(
        symbol="TSLA",
        underlyingSymbol="TSLA",
        assetCategory="STK",
        listingExchange="NASDAQ",
        buySell="SELL",
        quantity=-5.0,
        price=364.4400,
        tradeDate="20260402",
        dateTime="20260402;143102",
        orderTime="20260402;143045",
        orderId="1002",
        transactionId="0000e002.fake0002.01.02",
        execIds=["0000e002.fake0002.01.01", "0000e002.fake0002.01.02"],
        accountId="UXXXXXXX",
        commission=1.78,
        commissionCurrency="USD",
        currency="USD",
        orderType="LMT",
        fillCount=2,
    ),
    Trade(
        symbol="MSFT",
        underlyingSymbol="MSFT",
        assetCategory="STK",
        listingExchange="NYSE",
        buySell="BUY",
        quantity=3.0,
        price=425.1000,
        tradeDate="20260402",
        dateTime="20260402;143230",
        orderTime="20260402;143210",
        orderId="1003",
        transactionId="0000e003.fake0003.01.01",
        execIds=["0000e003.fake0003.01.01"],
        accountId="UXXXXXXX",
        commission=0.65,
        commissionCurrency="USD",
        currency="USD",
        orderType="MKT",
        fillCount=1,
    ),
]


def run(args):
    load_env()

    suffix = "" if args.poller == "1" else "_2"

    url = env(f"TARGET_WEBHOOK_URL{suffix}", "")
    secret = env(f"WEBHOOK_SECRET{suffix}", "")
    header_name = env(f"WEBHOOK_HEADER_NAME{suffix}", "")
    header_value = env(f"WEBHOOK_HEADER_VALUE{suffix}", "")

    if not url:
        die(f"TARGET_WEBHOOK_URL{suffix} is not set in .env")
    if not secret:
        die(f"WEBHOOK_SECRET{suffix} is not set in .env")

    payload = WebhookPayload(trades=SAMPLE_TRADES, errors=[])
    body = payload.model_dump_json(indent=2)

    signature = hmac.new(
        secret.encode(), body.encode(), hashlib.sha256
    ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-Signature-256": f"sha256={signature}",
    }
    if header_name:
        headers[header_name] = header_value

    print(f"Sending 3 sample trades to {url}")

    req = urllib.request.Request(url, data=body.encode(), method="POST")
    for k, v in headers.items():
        req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"Response: {resp.status} {resp.reason}")
            resp_body = resp.read().decode()
            if resp_body:
                print(resp_body)
    except urllib.error.HTTPError as e:
        print(f"HTTP error: {e.code} {e.reason}")
        print(e.read().decode())
    except urllib.error.URLError as e:
        die(f"Connection failed: {e.reason}")
