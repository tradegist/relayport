"""Tests for flex_fetch — error handling and None-return contract."""

import logging
import unittest
from unittest.mock import MagicMock, patch

import httpx

from .flex_fetch import _RedactTokenFilter, fetch_flex_report

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
        self.filt = _RedactTokenFilter()

    def _make_record(self, msg: str, args: tuple[object, ...] = ()) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test", level=logging.DEBUG, pathname="", lineno=0,
            msg=msg, args=args, exc_info=None,
        )
        return record

    def test_redacts_token_in_msg(self) -> None:
        record = self._make_record(
            "GET https://example.com/GetStatement?t=SECRET&q=REF&v=3"
        )
        self.filt.filter(record)
        self.assertNotIn("SECRET", record.msg)
        self.assertIn("t=REDACTED", record.msg)

    def test_redacts_token_in_args(self) -> None:
        record = self._make_record("request url: %s", ("https://x.com?t=SECRET",))
        self.filt.filter(record)
        self.assertNotIn("SECRET", str(record.args))

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

    def test_send_request_http_error_returns_none(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.return_value = _mock_response("", status_code=500)
        result = fetch_flex_report("tok", "qid")
        self.assertIsNone(result)

    def test_send_request_status_fail_returns_none(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.return_value = _mock_response(_SEND_FAIL)
        result = fetch_flex_report("tok", "qid")
        self.assertIsNone(result)

    def test_send_request_invalid_xml_returns_none(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.return_value = _mock_response("not xml at all <<<")
        result = fetch_flex_report("tok", "qid")
        self.assertIsNone(result)

    def test_send_request_missing_reference_code_returns_none(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.return_value = _mock_response(_SEND_OK_NO_REF)
        result = fetch_flex_report("tok", "qid")
        self.assertIsNone(result)

    def test_send_request_network_error_returns_none(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.side_effect = httpx.ConnectError("connection refused")
        result = fetch_flex_report("tok", "qid")
        self.assertIsNone(result)

    # ── Step 2 errors ───────────────────────────────────────────────

    def test_get_statement_http_error_returns_none(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.side_effect = [
            _mock_response(_SEND_OK),
            _mock_response("", status_code=503),
        ]
        result = fetch_flex_report("tok", "qid")
        self.assertIsNone(result)

    def test_get_statement_flex_error_returns_none(
        self, mock_get: MagicMock, _sleep: MagicMock,
    ) -> None:
        mock_get.side_effect = [
            _mock_response(_SEND_OK),
            _mock_response(_GET_ERROR),
        ]
        result = fetch_flex_report("tok", "qid")
        self.assertIsNone(result)

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
        result = fetch_flex_report("tok", "qid")
        self.assertIsNone(result)
