#!/usr/bin/env python3
import argparse
import importlib
import sys

import cli  # triggers set_config()
from cli.core import CORE_MODULES, register_parsers

# Project-specific command → module mapping
_PROJECT_MODULES: dict[str, str] = {
    "poll": "cli.poll",
    "reset-db": "cli.reset_db",
    "test-webhook": "cli.test_webhook",
    "watermark-reset": "cli.watermark",
}


def main():
    parser = argparse.ArgumentParser(
        description="RelayPort CLI",
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
                   help="Stream container logs alongside the poll")
    p.add_argument("--replay", type=int, metavar="N",
                   help="Resend N trades even if already processed (for testing)")

    p = sub.add_parser("reset-db", help="Drop dedup and metadata tables (fresh state)")
    p.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    p = sub.add_parser("test-webhook", help="Send sample trades to webhook endpoint")
    p.add_argument("poller", nargs="?", default="1", choices=["1", "2"],
                   help="Which poller's webhook URL (default: 1)")

    p = sub.add_parser("watermark-reset",
                       help="Reset timestamp watermark to now (all poller indices)")
    p.add_argument("relays", nargs="*", metavar="RELAY",
                   help="Relay name(s) to reset (default: all)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Dynamic dispatch: each command module must expose a run(args) function.
    modules = {**CORE_MODULES, **_PROJECT_MODULES}
    module = importlib.import_module(modules[args.command])
    module.run(args)


if __name__ == "__main__":
    main()
