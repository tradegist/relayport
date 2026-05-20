import logging
import re

import httpx

from market_data.errors import YahooError
from market_data.yahoo_client.types import YahooSession

YAHOO_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

log = logging.getLogger(__name__)


def _handle_consent_flow(body: str, client: httpx.Client) -> None:
    action_match = re.search(r'action="([^"]*collectConsent[^"]*)"', body)
    action_url = action_match.group(1).replace("&amp;", "&") if action_match else None
    if not action_url:
        log.debug(
            "Yahoo consent form not parseable — proceeding with current cookies",
            extra={"body_excerpt": body[:500]},
        )
        return

    hidden_fields: dict[str, str] = {}
    for input_match in re.finditer(r'<input(?=[^>]*type="hidden")[^>]*', body, re.IGNORECASE):
        tag = input_match.group(0)
        name_match = re.search(r'\bname="([^"]*)"', tag)
        value_match = re.search(r'\bvalue="([^"]*)"', tag)
        if name_match and value_match:
            hidden_fields[name_match.group(1)] = value_match.group(1)

    client.post(
        action_url,
        data={**hidden_fields, "agree": "agree"},
        headers={
            "User-Agent": YAHOO_USER_AGENT,
            "Origin": "https://guce.yahoo.com",
            "Referer": "https://guce.yahoo.com/",
        },
    )
    log.debug("Yahoo consent POST completed")


def get_yahoo_session() -> YahooSession:
    with httpx.Client(follow_redirects=True) as client:
        page_res = client.get(
            "https://finance.yahoo.com/quote/AAPL",
            headers={
                "User-Agent": YAHOO_USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        body = page_res.text
        log.debug("Yahoo Finance page fetched", extra={"status": str(page_res.status_code)})

        if 'action="' in body and "collectConsent" in body:
            log.debug("Yahoo consent page detected — handling automatically")
            _handle_consent_flow(body, client)

        crumb_res = client.get(
            "https://query2.finance.yahoo.com/v1/test/getcrumb",
            headers={"User-Agent": YAHOO_USER_AGENT},
        )
        crumb = crumb_res.text.strip()

        if not crumb or crumb.startswith("{"):
            log.debug(
                "Yahoo crumb fetch failed",
                extra={"status": str(crumb_res.status_code), "body": crumb[:200]},
            )
            raise YahooError("Failed to obtain a valid Yahoo Finance crumb")

        cookie_string = "; ".join(
            f"{name}={value}" for name, value in client.cookies.items()
        )
        log.debug("Yahoo Finance session established")
        return YahooSession(cookie_string=cookie_string, crumb=crumb)
