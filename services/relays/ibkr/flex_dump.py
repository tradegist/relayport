"""CLI entrypoint: fetch a live IBKR Flex report and write it to disk.

Run as:  python -m relays.ibkr.flex_dump --token TOKEN --query-id ID [--dump PATH]
"""

import argparse
import logging
import sys
from pathlib import Path

from relays.ibkr.flex_fetch import RedactTokenFilter, fetch_flex_report

_DEFAULT_DUMP_PATH = "services/relays/ibkr/fixtures/raw.xml"

log = logging.getLogger("relays.ibkr.flex_dump")


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Dump an IBKR Flex report (Activity or Trade Confirmation) to disk.",
    )
    parser.add_argument("--token", required=True, help="IBKR Flex token.")
    parser.add_argument("--query-id", required=True, dest="query_id", help="IBKR Flex query ID.")
    parser.add_argument(
        "--lookback-days", type=int, default=None, dest="lookback_days",
        metavar="N",
        help="Override the saved query's Period with last N calendar days (1-365).",
    )
    parser.add_argument(
        "--dump", metavar="PATH", default=_DEFAULT_DUMP_PATH,
        help=f"Output file path (default: {_DEFAULT_DUMP_PATH}).",
    )
    args = parser.parse_args()

    if args.lookback_days is not None and not 1 <= args.lookback_days <= 365:
        parser.error("--lookback-days must be between 1 and 365")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Filters attached to a Logger are not consulted for records emitted by
    # child loggers (e.g. httpx) — only Handler filters are.  Attach to the
    # root handler so every logger's records are redacted before output.
    redactor = RedactTokenFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)

    xml = fetch_flex_report(
        flex_token=args.token,
        flex_query_id=args.query_id,
        lookback_days=args.lookback_days,
    )
    if xml is None:
        sys.exit("Flex fetch failed — see log output above")

    path = Path(args.dump)
    path.write_text(xml)
    log.info("Wrote %d bytes to %s", len(xml), path)


if __name__ == "__main__":
    _main()
