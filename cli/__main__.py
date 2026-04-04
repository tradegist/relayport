#!/usr/bin/env python3
import argparse
import importlib
import sys


def main():
    parser = argparse.ArgumentParser(
        description="IBKR Webhook Relay CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("deploy", help="Deploy infrastructure (Terraform + Docker)")
    sub.add_parser("destroy", help="Permanently destroy all infrastructure")
    sub.add_parser("pause", help="Snapshot droplet + delete (save costs)")
    sub.add_parser("resume", help="Restore droplet from snapshot")

    p = sub.add_parser("sync", help="Push .env + restart services")
    p.add_argument("services", nargs="*", help="Services to restart (default: all)")

    p = sub.add_parser("poll", help="Trigger an immediate Flex poll")
    p.add_argument("poller", nargs="?", default="1", choices=["1", "2"],
                   help="Which poller (default: 1)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Run poll via SSH to see full poller logs")
    p.add_argument("--debug", action="store_true",
                   help="Dump raw Flex XML (implies -v)")
    p.add_argument("--replay", type=int, metavar="N",
                   help="Resend N trades even if already processed (for testing)")

    p = sub.add_parser("test-webhook", help="Send sample trades to webhook endpoint")
    p.add_argument("poller", nargs="?", default="1", choices=["1", "2"],
                   help="Which poller's webhook URL (default: 1)")

    p = sub.add_parser("order", help="Place a stock order")
    p.add_argument("quantity", type=int, help="Positive=BUY, negative=SELL")
    p.add_argument("symbol", help="Ticker symbol")
    p.add_argument("order_type", choices=["MKT", "LMT", "mkt", "lmt"],
                   help="Order type")
    p.add_argument("limit_price", nargs="?", type=float,
                   help="Limit price (required for LMT)")
    p.add_argument("currency", nargs="?", default="USD",
                   help="Currency (default: USD)")
    p.add_argument("exchange", nargs="?", default="SMART",
                   help="Exchange (default: SMART)")
    p.add_argument("--tif", default="DAY",
                   choices=["DAY", "GTC", "IOC", "GTD", "OPG", "FOK", "DTC"],
                   help="Time in force (default: DAY)")
    p.add_argument("--outside-rth", action="store_true",
                   help="Allow execution outside regular trading hours")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    modules = {
        "deploy": "cli.deploy",
        "destroy": "cli.destroy",
        "pause": "cli.pause",
        "resume": "cli.resume",
        "sync": "cli.sync",
        "poll": "cli.poll",
        "order": "cli.order",
        "test-webhook": "cli.test_webhook",
    }

    module = importlib.import_module(modules[args.command])
    module.run(args)


if __name__ == "__main__":
    main()
