"""IBKR Flex Web Service — two-step report fetcher."""

import logging
import re
import time
import xml.etree.ElementTree as ET

import httpx

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


def fetch_flex_report(
    flex_token: str,
    flex_query_id: str,
    *,
    lookback_days: int | None = None,
) -> str | None:
    """Two-step Flex Web Service: SendRequest -> GetStatement.

    *lookback_days* (1-365) overrides the saved query's Period via the
    documented ``p`` URL param so IBKR returns the last N calendar days
    regardless of how the query is configured server-side.  ``None``
    (default) lets the saved query Period apply.

    Returns the raw XML text on success, or ``None`` on any HTTP/XML
    error.  Raises ``ValueError`` if *lookback_days* is outside 1-365.
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


