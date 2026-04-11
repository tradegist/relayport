#!/usr/bin/env python3
import argparse
import importlib
import sys

import cli  # triggers set_config()
from cli.core import CORE_MODULES, register_parsers

# Project-specific command → module mapping
_PROJECT_MODULES: dict[str, str] = {
    "poll": "cli.poll",
    "test-webhook": "cli.test_webhook",
}


def main():
    parser = argparse.ArgumentParser(
        description="IBKR Webhook Relay CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # ── Core commands (shared across projects) ──
    register_parsers(sub)

    # ── Project-specific commands ──
    p = sub.add_parser("poll", help="Trigger an immediate poll")
    p.add_argument("relay", help="Relay name (e.g. ibkr)")
    p.add_argument("poll_idx", nargs="?", default="1", type=str,
                   help="Poller index (default: 1)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Run poll via SSH to see full poller logs")
    p.add_argument("--debug", action="store_true",
                   help="Dump raw Flex XML (implies -v)")
    p.add_argument("--replay", type=int, metavar="N",
                   help="Resend N trades even if already processed (for testing)")

    p = sub.add_parser("test-webhook", help="Send sample trades to webhook endpoint")
    p.add_argument("poller", nargs="?", default="1", choices=["1", "2"],
                   help="Which poller's webhook URL (default: 1)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    modules = {**CORE_MODULES, **_PROJECT_MODULES}
    module = importlib.import_module(modules[args.command])
    module.run(args)


if __name__ == "__main__":
    main()
