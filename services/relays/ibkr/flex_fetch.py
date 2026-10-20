"""IBKR Flex Web Service — two-step report fetcher."""

import logging
import re
import time
import xml.etree.ElementTree as ET

import httpx

from relay_core import FetchError

log = logging.getLogger("relays.ibkr.flex_fetch")

_FLEX_TOKEN_RE = re.compile(
    r"(ndcdyn\.interactivebrokers\.com/[^\s]*[?&])t=[^&\s]+",
)


def _redact_token(text: str) -> str:
    """Replace the ``t=`` query-param value in IBKR Flex URLs so tokens stay out of logs."""
    return _FLEX_TOKEN_RE.sub(r"\1t=REDACTED", text)


class RedactTokenFilter(logging.Filter):
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
# Seconds to wait before each GetStatement attempt while IBKR generates the report.
_GET_STATEMENT_WAITS_S = (5, 10, 15, 30)


def fetch_flex_report(
    flex_token: str,
    flex_query_id: str,
    *,
    lookback_days: int | None = None,
) -> str:
    """Two-step Flex Web Service: SendRequest -> GetStatement.

    *lookback_days* (1-365) overrides the saved query's Period via the
    documented ``p`` URL param so IBKR returns the last N calendar days
    regardless of how the query is configured server-side.  ``None``
    (default) lets the saved query Period apply.

    Returns the raw XML text on success.

    Raises:
        FetchError: On any HTTP, XML or Flex-level error (expired token,
            invalid query, generation timeout). The message names the
            failing step and IBKR's error code, with the token redacted.
        ValueError: If *lookback_days* is outside 1-365.
    """
    if lookback_days is not None and not 1 <= lookback_days <= 365:
        raise ValueError(
            f"lookback_days must be between 1 and 365, got {lookback_days!r}"
        )

    headers = {"User-Agent": USER_AGENT}
    params: dict[str, str] = {"t": flex_token, "q": flex_query_id, "v": "3"}
    if lookback_days is not None:
        params["p"] = str(lookback_days)

    try:
        # Step 1: request report generation
        resp = httpx.get(
            f"{FLEX_BASE}/SendRequest",
            params=params,
            headers=headers,
            timeout=30.0,
        )
        resp.raise_for_status()

        root = ET.fromstring(resp.text)
        if root.findtext("Status") != "Success":
            code = root.findtext("ErrorCode", "?")
            msg = root.findtext("ErrorMessage", "Unknown error")
            raise FetchError(f"Flex SendRequest failed: [{code}] {msg}")

        ref_code = root.findtext("ReferenceCode")
        if not ref_code:
            raise FetchError("Flex SendRequest succeeded but returned no ReferenceCode")
        log.debug("SendRequest OK — ref=%s, waiting for report...", ref_code)

        # Step 2: poll for the generated report
        for wait in _GET_STATEMENT_WAITS_S:
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
                raise FetchError(f"Flex GetStatement failed: [{err_code}] {msg}")

            return str(resp.text)

        raise FetchError(
            f"Flex report still generating after {sum(_GET_STATEMENT_WAITS_S)}s"
            f" of GetStatement retries"
        )
    except (httpx.HTTPError, ET.ParseError) as exc:
        # `from None`: the original exception's message carries the token
        # in the request URL, and a chained traceback would print it.
        raise FetchError(
            f"Flex report fetch failed: {_redact_token(str(exc))}"
        ) from None


