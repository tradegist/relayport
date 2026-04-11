"""Send a simulated webhook payload with 3 sample trades."""

import hashlib
import hmac
import urllib.error
import urllib.request

from cli.core import die, env, load_env
from poller_models import BuySell, Trade, WebhookPayloadTrades

SAMPLE_TRADES = [
    Trade(
        source="flex",
        symbol="AAPL",
        assetClass="equity",
        side=BuySell.BUY,
        volume=10.0,
        price=187.5200,
        cost=1875.20,
        fee=1.0,
        timestamp="20260402;143015",
        orderId="1001",
        execIds=["0000e001.fake0001.01.01"],
        orderType="market",
        fillCount=1,
        raw={"assetCategory": "STK", "listingExchange": "NASDAQ", "currency": "USD"},
    ),
    Trade(
        source="flex",
        symbol="TSLA",
        assetClass="equity",
        side=BuySell.SELL,
        volume=-5.0,
        price=364.4400,
        cost=-1822.20,
        fee=1.78,
        timestamp="20260402;143102",
        orderId="1002",
        execIds=["0000e002.fake0002.01.01", "0000e002.fake0002.01.02"],
        orderType="limit",
        fillCount=2,
        raw={"assetCategory": "STK", "listingExchange": "NASDAQ", "currency": "USD"},
    ),
    Trade(
        source="flex",
        symbol="MSFT",
        assetClass="equity",
        side=BuySell.BUY,
        volume=3.0,
        price=425.1000,
        cost=1275.30,
        fee=0.65,
        timestamp="20260402;143230",
        orderId="1003",
        execIds=["0000e003.fake0003.01.01"],
        orderType="market",
        fillCount=1,
        raw={"assetCategory": "STK", "listingExchange": "NYSE", "currency": "USD"},
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

    payload = WebhookPayloadTrades(relay="ibkr", data=SAMPLE_TRADES, errors=[])
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
