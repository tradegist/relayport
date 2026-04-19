"""IBKR Flex Web Service — two-step report fetcher."""

import argparse
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

log = logging.getLogger("relays.ibkr.flex_fetch")

_FLEX_TOKEN_RE = re.compile(
    r"(ndcdyn\.interactivebrokers\.com/[^\s]*[?&])t=[^&\s]+",
)


def _redact_token(text: str) -> str:
    """Replace the ``t=`` query-param value in IBKR Flex URLs so tokens stay out of logs."""
    return _FLEX_TOKEN_RE.sub(r"\1t=REDACTED", text)


class _RedactTokenFilter(logging.Filter):
    """Strip Flex tokens from any log record that passes through."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_token(record.msg)
        if record.args:
            record.args = tuple(
                self._redact_arg(a)
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        return True

    @staticmethod
    def _redact_arg(arg: object) -> object:
        text = str(arg)
        redacted = _redact_token(text)
        if redacted is not text and redacted != text:
            return redacted
        return arg



FLEX_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
USER_AGENT = "ibkr-relay/1.0"


def fetch_flex_report(flex_token: str, flex_query_id: str) -> str | None:
    """Two-step Flex Web Service: SendRequest -> GetStatement.

    Returns the raw XML text on success, or ``None`` on any error.
    """
    headers = {"User-Agent": USER_AGENT}

    try:
        # Step 1: request report generation
        resp = httpx.get(
            f"{FLEX_BASE}/SendRequest",
            params={"t": flex_token, "q": flex_query_id, "v": "3"},
            headers=headers,
            timeout=30.0,
        )
        resp.raise_for_status()

        root = ET.fromstring(resp.text)
        if root.findtext("Status") != "Success":
            code = root.findtext("ErrorCode", "?")
            msg = root.findtext("ErrorMessage", "Unknown error")
            log.error("SendRequest failed: [%s] %s", code, msg)
            return None

        ref_code = root.findtext("ReferenceCode")
        if not ref_code:
            log.error("SendRequest succeeded but no ReferenceCode in response")
            return None
        log.debug("SendRequest OK — ref=%s, waiting for report...", ref_code)

        # Step 2: poll for the generated report
        for wait in (5, 10, 15, 30):
            time.sleep(wait)
            resp = httpx.get(
                f"{FLEX_BASE}/GetStatement",
                params={"t": flex_token, "q": ref_code, "v": "3"},
                headers=headers,
                timeout=60.0,
            )
            resp.raise_for_status()

            # Error responses are wrapped in <FlexStatementResponse>
            if resp.text.strip().startswith("<FlexStatementResponse"):
                err_root = ET.fromstring(resp.text)
                err_code = err_root.findtext("ErrorCode", "")
                if err_code == "1019":  # generation in progress
                    log.debug("Report still generating, retrying...")
                    continue
                msg = err_root.findtext("ErrorMessage", "Unknown error")
                log.error("GetStatement failed: [%s] %s", err_code, msg)
                return None

            return str(resp.text)

        log.error("Report generation timed out after retries")
        return None
    except (httpx.HTTPError, ET.ParseError) as exc:
        log.error("Flex report fetch failed: %s", _redact_token(str(exc)))
        return None


# ── CLI: dump a live Flex response for debugging / fixture capture ──────

def _main() -> None:
    """Fetch a Flex report using env-var credentials and write it to disk.

    Usage:  python -m relays.ibkr.flex_fetch --dump /tmp/raw.xml [--suffix _2]

    Reads ``IBKR_FLEX_TOKEN[suffix]`` and ``IBKR_FLEX_QUERY_ID[suffix]``
    from the environment.  Writes the raw XML response to ``--dump`` (or
    stdout when ``--dump`` is ``-`` / omitted).
    """
    parser = argparse.ArgumentParser(
        description="Dump an IBKR Flex report (Activity or Trade Confirmation) to disk.",
    )
    parser.add_argument(
        "--dump", metavar="PATH", default="-",
        help="Output path; '-' or omitted writes to stdout.",
    )
    parser.add_argument(
        "--suffix", default="",
        help="Env-var suffix, e.g. '_2' to read IBKR_FLEX_TOKEN_2 / IBKR_FLEX_QUERY_ID_2.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger().addFilter(_RedactTokenFilter())

    token = os.environ.get(f"IBKR_FLEX_TOKEN{args.suffix}", "").strip()
    query_id = os.environ.get(f"IBKR_FLEX_QUERY_ID{args.suffix}", "").strip()
    if not token:
        sys.exit(f"IBKR_FLEX_TOKEN{args.suffix} is not set")
    if not query_id:
        sys.exit(f"IBKR_FLEX_QUERY_ID{args.suffix} is not set")

    xml = fetch_flex_report(flex_token=token, flex_query_id=query_id)
    if xml is None:
        sys.exit("Flex fetch failed — see log output above")

    if args.dump == "-":
        sys.stdout.write(xml)
    else:
        path = Path(args.dump)
        path.write_text(xml)
        log.info("Wrote %d bytes to %s", len(xml), path)


if __name__ == "__main__":
    _main()
