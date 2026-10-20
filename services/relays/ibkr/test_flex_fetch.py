"""Tests for flex_fetch — error handling and FetchError contract."""

import logging
import unittest
from unittest.mock import MagicMock, patch

import httpx

from relay_core import FetchError

from .flex_fetch import RedactTokenFilter, fetch_flex_report

# ── XML helpers ─────────────────────────────────────────────────────

_SEND_OK = (
    "<FlexStatementResponse>"
    "<Status>Success</Status>"
    "<ReferenceCode>REF123</ReferenceCode>"
    "</FlexStatementResponse>"
)

_SEND_OK_NO_REF = (
    "<FlexStatementResponse>"
    "<Status>Success</Status>"
    "</FlexStatementResponse>"
)

_SEND_FAIL = (
    "<FlexStatementResponse>"
    "<Status>Fail</Status>"
    "<ErrorCode>1001</ErrorCode>"
    "<ErrorMessage>Invalid token</ErrorMessage>"
    "</FlexStatementResponse>"
)

_GET_STILL_GENERATING = (
    "<FlexStatementResponse>"
    "<ErrorCode>1019</ErrorCode>"
    "<ErrorMessage>Statement generation in progress</ErrorMessage>"
    "</FlexStatementResponse>"
)

_GET_ERROR = (
    "<FlexStatementResponse>"
    "<ErrorCode>1020</ErrorCode>"
    "<ErrorMessage>Too many requests</ErrorMessage>"
    "</FlexStatementResponse>"
)

_REPORT_XML = "<FlexQueryResponse><Trades></Trades></FlexQueryResponse>"


def _mock_response(text: str, status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.text = text
    resp.status_code = status_code
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ── Tests ───────────────────────────────────────────────────────────


class TestRedactTokenFilter(unittest.TestCase):
    """Token filter strips ``t=`` values from log records."""

    def setUp(self) -> None:
        self.filt = RedactTokenFilter()

    def _make_record(self, msg: str, args: tuple[object, ...] = ()) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test", level=logging.DEBUG, pathname="", lineno=0,
            msg=msg, args=args, exc_info=None,
        )
        return record

    def test_redacts_token_in_msg(self) -> None:
        record = self._make_record(
            "GET https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement?t=SECRET&q=REF&v=3"
        )
        self.filt.filter(record)
        self.assertNotIn("SECRET", record.msg)
        self.assertIn("t=REDACTED", record.msg)

    def test_redacts_token_in_args(self) -> None:
        record = self._make_record(
            "request url: %s",
            ("https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest?t=SECRET&q=123&v=3",),
        )
        self.filt.filter(record)
        self.assertNotIn("SECRET", str(record.args))

    def test_redacts_token_in_non_str_args(self) -> None:
        """httpx passes URL as httpx.URL (not str) — filter must handle it."""
        url = httpx.URL(
            "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement?t=SECRET&q=REF&v=3"
        )
        record = self._make_record("HTTP Request: %s %s", ("GET", url))
        self.filt.filter(record)
        formatted = record.msg % record.args
        self.assertNotIn("SECRET", formatted)
        self.assertIn("t=REDACTED", formatted)

    def test_ignores_non_ibkr_urls(self) -> None:
        record = self._make_record("GET https://example.com/api?t=keep_this&v=1")
        self.filt.filter(record)
        self.assertIn("t=keep_this", record.msg)

    def test_passes_through_unrelated_messages(self) -> None:
        record = self._make_record("no token here")
        self.filt.filter(record)
        self.assertEqual(record.msg, "no token here")


@patch("relays.ibkr.flex_fetch.time.sleep", return_value=None)
@patch("relays.ibkr.flex_fetch.httpx.get")
class TestFetchFlexReport(unittest.TestCase):
    """Test fetch_flex_report error handling."""

    # ── Happy path ──────────────────────────────────────────────────

    def test_success_returns_report_xml(self, mock_get: MagicMock, _sleep: MagicMock) -> None:
        mock_get.side_effect = [
            _mock_response(_SEND_OK),
            _mock_response(_REPORT_XML),
        ]
        result = fetch_flex_report("tok", "qid")
        self.assertEqual(result, _REPORT_XML)

    # ── Step 1 errors ───────────────────────────────────────────────

    def test_send_request_http_error_raises(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.return_value = _mock_response("", status_code=500)
        with self.assertRaisesRegex(FetchError, "Flex report fetch failed: error"):
            fetch_flex_report("tok", "qid")

    def test_send_request_status_fail_raises_with_ibkr_code(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.return_value = _mock_response(_SEND_FAIL)
        with self.assertRaisesRegex(FetchError, r"Flex SendRequest failed: \[1001\] Invalid token"):
            fetch_flex_report("tok", "qid")

    def test_send_request_invalid_xml_raises(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.return_value = _mock_response("not xml at all <<<")
        with self.assertRaisesRegex(FetchError, "Flex report fetch failed: syntax error"):
            fetch_flex_report("tok", "qid")

    def test_send_request_missing_reference_code_raises(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.return_value = _mock_response(_SEND_OK_NO_REF)
        with self.assertRaisesRegex(FetchError, "no ReferenceCode"):
            fetch_flex_report("tok", "qid")

    def test_send_request_network_error_raises(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.side_effect = httpx.ConnectError("connection refused")
        with self.assertRaisesRegex(FetchError, "Flex report fetch failed: connection refused"):
            fetch_flex_report("tok", "qid")

    def test_http_error_message_redacts_token_and_drops_cause(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        url = (
            "https://ndcdyn.interactivebrokers.com/AccountManagement/"
            "FlexWebService/SendRequest?t=SECRET123&q=qid&v=3"
        )
        mock_get.side_effect = httpx.ConnectError(f"Failed to connect: {url}")
        with self.assertRaises(FetchError) as ctx:
            fetch_flex_report("SECRET123", "qid")
        self.assertNotIn("SECRET123", str(ctx.exception))
        self.assertIn("t=REDACTED", str(ctx.exception))
        # A chained cause would print the unredacted URL in any traceback.
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)

    # ── Step 2 errors ───────────────────────────────────────────────

    def test_get_statement_http_error_raises(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.side_effect = [
            _mock_response(_SEND_OK),
            _mock_response("", status_code=503),
        ]
        with self.assertRaisesRegex(FetchError, "Flex report fetch failed: error"):
            fetch_flex_report("tok", "qid")

    def test_get_statement_flex_error_raises_with_ibkr_code(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.side_effect = [
            _mock_response(_SEND_OK),
            _mock_response(_GET_ERROR),
        ]
        with self.assertRaisesRegex(FetchError, r"Flex GetStatement failed: \[1020\] Too many requests"):
            fetch_flex_report("tok", "qid")

    def test_get_statement_retries_on_1019_then_succeeds(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.side_effect = [
            _mock_response(_SEND_OK),
            _mock_response(_GET_STILL_GENERATING),
            _mock_response(_REPORT_XML),
        ]
        result = fetch_flex_report("tok", "qid")
        self.assertEqual(result, _REPORT_XML)

    def test_get_statement_timeout_after_all_retries(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.side_effect = [
            _mock_response(_SEND_OK),
            _mock_response(_GET_STILL_GENERATING),
            _mock_response(_GET_STILL_GENERATING),
            _mock_response(_GET_STILL_GENERATING),
            _mock_response(_GET_STILL_GENERATING),
        ]
        with self.assertRaisesRegex(FetchError, "still generating after 60s"):
            fetch_flex_report("tok", "qid")

    # ── lookback_days override ──────────────────────────────────────

    def test_lookback_days_adds_p_param(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.side_effect = [
            _mock_response(_SEND_OK),
            _mock_response(_REPORT_XML),
        ]
        fetch_flex_report("tok", "qid", lookback_days=40)

        send_call = mock_get.call_args_list[0]
        params = send_call.kwargs["params"]
        self.assertEqual(params["p"], "40")
        self.assertEqual(params["t"], "tok")
        self.assertEqual(params["q"], "qid")
        self.assertEqual(params["v"], "3")
        # Step 2 (GetStatement) reuses ref_code, no p override
        get_call = mock_get.call_args_list[1]
        self.assertNotIn("p", get_call.kwargs["params"])

    def test_no_lookback_omits_p_param(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.side_effect = [
            _mock_response(_SEND_OK),
            _mock_response(_REPORT_XML),
        ]
        fetch_flex_report("tok", "qid")

        params = mock_get.call_args_list[0].kwargs["params"]
        self.assertNotIn("p", params)

    def test_lookback_days_below_minimum_raises(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        with self.assertRaises(ValueError):
            fetch_flex_report("tok", "qid", lookback_days=0)
        mock_get.assert_not_called()

    def test_lookback_days_above_maximum_raises(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        with self.assertRaises(ValueError):
            fetch_flex_report("tok", "qid", lookback_days=366)
        mock_get.assert_not_called()
