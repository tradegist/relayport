import logging
import time
from datetime import UTC, datetime
from urllib.parse import quote

from curl_cffi import requests as cffi_requests

from market_data.errors import YahooError
from market_data.yahoo_client.auth import _IMPERSONATE, API_HEADERS, get_yahoo_session
from market_data.yahoo_client.types import DividendInfo, YahooSession

_MAX_RETRIES = 3
_RETRY_DELAY_SECONDS = 3.0
_SECONDS_PER_YEAR = 365.25 * 24 * 60 * 60

log = logging.getLogger(__name__)


def _to_date_string(unix_seconds: float) -> str:
    return datetime.fromtimestamp(unix_seconds, tz=UTC).strftime("%Y-%m-%d")


def _is_future_unix(unix: object) -> bool:
    return isinstance(unix, (int, float)) and float(unix) > time.time()


def fetch_dividend_info_from_yahoo(ticker: str, session: YahooSession) -> DividendInfo:
    headers = {
        **API_HEADERS,
        "Cookie": session.cookie_string,
        "Referer": f"https://finance.yahoo.com/quote/{quote(ticker)}",
    }
    qs = f"&crumb={quote(session.crumb)}"

    with cffi_requests.Session(impersonate=_IMPERSONATE) as client:
        # ── 1. Summary: announced dates + annualised dividend rate ───────
        summary_res = client.get(
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{quote(ticker)}"
            f"?modules=calendarEvents,summaryDetail{qs}",
            headers=headers,
        )

        if summary_res.status_code == 401:
            raise YahooError(
                f"Yahoo Finance 401 for {ticker}",
                status_code=401,
                error_code="YAHOO_UNAUTHORIZED",
            )
        if summary_res.status_code != 200:
            raise YahooError(
                f"Yahoo Finance quoteSummary HTTP {summary_res.status_code} for {ticker}",
                status_code=summary_res.status_code,
            )

        summary_json = summary_res.json()
        result_list = (summary_json.get("quoteSummary") or {}).get("result") or []
        result = result_list[0] if result_list else None
        cal = result.get("calendarEvents") if result else None
        dps_raw = (result.get("summaryDetail") or {}).get("dividendRate") if result else None
        annual_dps_from_rate: float | None = dps_raw.get("raw") if isinstance(dps_raw, dict) else None

        announced_ex_div_unix = (cal.get("exDividendDate") or {}).get("raw") if cal else None
        announced_payment_unix = (cal.get("dividendDate") or {}).get("raw") if cal else None

        # ── 2. Chart: always fetch for per-payment dps + frequency ───────
        # dps (per-payment) comes from the most recent historical dividend event.
        # annual_dps comes from Yahoo's dividendRate when available; otherwise
        # it is estimated as per_payment_dps * payments_per_year derived from
        # the average gap between the last five events. This mirrors yfinance's
        # own split: dividendRate (annualised) vs historical dividends (per-payment).
        chart_res = client.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker)}"
            f"?interval=1d&range=2y&events=dividends{qs}",
            headers=headers,
        )

        if chart_res.status_code == 401:
            raise YahooError(
                f"Yahoo Finance 401 for {ticker}",
                status_code=401,
                error_code="YAHOO_UNAUTHORIZED",
            )

        per_payment_dps: float | None = None
        avg_gap_seconds: float | None = None
        estimated_ex_div: float | None = None
        payment_offset_seconds: float = 21 * 24 * 60 * 60

        if chart_res.status_code == 200:
            chart_json = chart_res.json()
            chart_result_list = (chart_json.get("chart") or {}).get("result") or []
            div_events: dict[str, dict[str, object]] | None = (
                (chart_result_list[0] if chart_result_list else {}).get("events") or {}
            ).get("dividends")

            if div_events:
                sorted_keys = sorted(div_events.keys(), key=float)
                if len(sorted_keys) >= 2:
                    gaps: list[float] = []
                    prev: float | None = None
                    last_key: str | None = None
                    for key in sorted_keys[-5:]:
                        curr = float(key)
                        if prev is not None:
                            gaps.append(curr - prev)
                        prev = curr
                        last_key = key

                    if last_key is not None:
                        avg_gap_seconds = sum(gaps) / len(gaps)

                        last_event = div_events.get(last_key) or {}
                        last_event_amount = last_event.get("amount")
                        per_payment_dps = (
                            float(last_event_amount)
                            if isinstance(last_event_amount, (int, float))
                            else None
                        )

                        now = time.time()
                        estimated_ex_div = float(last_key)
                        while estimated_ex_div <= now:
                            estimated_ex_div += avg_gap_seconds

                        if (
                            _is_future_unix(announced_payment_unix)
                            and _is_future_unix(announced_ex_div_unix)
                        ):
                            payment_offset_seconds = float(announced_payment_unix) - float(announced_ex_div_unix)  # type: ignore[arg-type]

        # ── 3. Derive annual_dps ─────────────────────────────────────────
        if annual_dps_from_rate is not None:
            annual_dps: float | None = annual_dps_from_rate
        elif per_payment_dps is not None and avg_gap_seconds is not None:
            annual_dps = per_payment_dps * (_SECONDS_PER_YEAR / avg_gap_seconds)
        else:
            annual_dps = None

        # ── 4. Return: announced dates take priority over estimated ──────
        if _is_future_unix(announced_ex_div_unix) and _is_future_unix(announced_payment_unix):
            return DividendInfo(
                ex_div_date=_to_date_string(float(announced_ex_div_unix)),  # type: ignore[arg-type]
                payment_date=_to_date_string(float(announced_payment_unix)),  # type: ignore[arg-type]
                dps=per_payment_dps,
                annual_dps=annual_dps,
                are_dates_estimated=False,
            )

        if estimated_ex_div is not None:
            return DividendInfo(
                ex_div_date=_to_date_string(estimated_ex_div),
                payment_date=_to_date_string(estimated_ex_div + payment_offset_seconds),
                dps=per_payment_dps,
                annual_dps=annual_dps,
                are_dates_estimated=True,
            )

        return DividendInfo(
            ex_div_date=None,
            payment_date=None,
            dps=per_payment_dps,
            annual_dps=annual_dps,
            are_dates_estimated=False,
        )


def fetch_with_retry(
    ticker: str, session: YahooSession, attempt: int = 0
) -> tuple[DividendInfo, YahooSession]:
    try:
        info = fetch_dividend_info_from_yahoo(ticker, session)
        return info, session
    except YahooError as e:
        if e.error_code == "YAHOO_UNAUTHORIZED" and attempt < _MAX_RETRIES:
            log.debug(
                "Yahoo 401 for %s — refreshing session (attempt %d/%d)",
                ticker,
                attempt + 1,
                _MAX_RETRIES,
            )
            time.sleep(_RETRY_DELAY_SECONDS)
            fresh_session = get_yahoo_session()
            return fetch_with_retry(ticker, fresh_session, attempt + 1)
        raise
