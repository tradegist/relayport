"""Yahoo Finance session management — fragile by nature.

Yahoo does not offer a public API. This module reverse-engineers their
authentication flow and WILL break whenever Yahoo changes it.

If requests start returning 429 or empty crumbs, check how yfinance handles
it first — they track Yahoo's auth changes closely and fix them quickly:
  https://github.com/ranaroussi/yfinance/blob/main/yfinance/data.py
  (look at YfData._get_cookie_basic, _get_crumb_basic, _get_cookie_and_crumb)

Key invariants that Yahoo currently requires (as of May 2026):
- TLS fingerprint must match a real browser (Chrome). Plain httpx/requests
  have a Python fingerprint that Yahoo's WAF blocks with 429. We use
  curl_cffi with impersonate="chrome120" to pass this check — the same
  technique yfinance uses internally.
- Session cookies (A1/A3) must be established by visiting finance.yahoo.com
  before hitting the crumb endpoint. The crumb endpoint returns 429 without them.
- GDPR regions may require a consent form POST before cookies are set.
"""
import logging
import re
from typing import Any, Literal

from curl_cffi import requests as cffi_requests

from market_data.errors import ErrorCode, YahooError
from market_data.yahoo_client.types import YahooSession

_PAGE_URL = "https://finance.yahoo.com/"
_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"

IMPERSONATE: Literal["chrome120"] = "chrome120"

_PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

API_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin": "https://finance.yahoo.com",
    "Referer": "https://finance.yahoo.com/",
}

log = logging.getLogger(__name__)


def _handle_consent(session: cffi_requests.Session[Any], body: str, page_url: str) -> None:
    """POST agree to Yahoo GDPR consent form already loaded in `body`."""
    action_match = re.search(r'action="([^"]*collectConsent[^"]*)"', body)
    action_url = action_match.group(1).replace("&amp;", "&") if action_match else None
    if not action_url:
        log.debug("Yahoo consent form not parseable — proceeding with current cookies")
        return

    hidden_fields: dict[str, str] = {}
    for input_match in re.finditer(r'<input(?=[^>]*type="hidden")[^>]*', body, re.IGNORECASE):
        tag = input_match.group(0)
        name_match = re.search(r'\bname="([^"]*)"', tag)
        value_match = re.search(r'\bvalue="([^"]*)"', tag)
        if name_match and value_match:
            hidden_fields[name_match.group(1)] = value_match.group(1)

    session.post(
        action_url,
        data={**hidden_fields, "agree": "agree"},
        headers={
            **API_HEADERS,
            "Origin": "https://guce.yahoo.com",
            "Referer": page_url,
        },
        impersonate=IMPERSONATE,
    )
    log.debug("Yahoo consent POST completed")


def get_yahoo_session() -> YahooSession:
    """Establish a Yahoo Finance session.

    1. Visit finance.yahoo.com to acquire session cookies (same as a browser).
       Uses Chrome TLS impersonation so Yahoo's WAF accepts the request.
    2. Handle GDPR consent redirect if present.
    3. Fetch the crumb from query1 using those cookies.
    """
    with cffi_requests.Session(impersonate=IMPERSONATE) as session:
        page_res = session.get(_PAGE_URL, headers=_PAGE_HEADERS)
        log.debug("Yahoo Finance page fetched: HTTP %s", page_res.status_code)

        body = page_res.text
        if 'action="' in body and "collectConsent" in body:
            log.debug("Yahoo consent page detected — handling automatically")
            _handle_consent(session, body, str(page_res.url))

        crumb_res = session.get(_CRUMB_URL, headers=API_HEADERS)

        if crumb_res.status_code != 200:
            raise YahooError(
                f"Yahoo Finance crumb endpoint returned HTTP {crumb_res.status_code}",
                ErrorCode.YAHOO_ERROR,
            )

        crumb = crumb_res.text.strip()
        if not crumb or crumb.startswith("{"):
            raise YahooError("Failed to obtain a valid Yahoo Finance crumb", ErrorCode.YAHOO_ERROR)

        cookie_string = "; ".join(
            f"{name}={value}" for name, value in session.cookies.items()
        )
        log.debug("Yahoo Finance session established")
        return YahooSession(cookie_string=cookie_string, crumb=crumb)
