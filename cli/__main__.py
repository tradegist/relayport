#!/usr/bin/env python3
import argparse
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

    p = sub.add_parser("order", help="Place an order")
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

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    from cli import deploy, sync, pause, resume, destroy, poll, order

    commands = {
        "deploy": deploy.run,
        "destroy": destroy.run,
        "pause": pause.run,
        "resume": resume.run,
        "sync": sync.run,
        "poll": poll.run,
        "order": order.run,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()
