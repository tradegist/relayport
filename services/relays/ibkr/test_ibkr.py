"""Tests for the IBKR relay adapter."""

import json
import os
import unittest
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from relay_core import get_debounce_ms, get_poll_interval, is_listener_enabled
from relays.ibkr import (
    _build_connect,
    _build_poller_configs,
    _event_filter,
    _get_account_timezone,
    _get_bridge_api_token,
    _get_bridge_ws_url,
    _get_flex_lookback_days,
    _get_flex_query_id,
    _get_flex_token,
    _is_exec_events_enabled,
    _map_fill,
    _on_message_factory,
    build_relay,
)
from shared import BuySell

from .bridge_models import (
    WsCommissionReport,
    WsContract,
    WsEnvelope,
    WsEventType,
    WsExecution,
    WsFill,
)

_TEST_TZ = ZoneInfo("UTC")

# ── Env var setup ────────────────────────────────────────────────────

_ORIG_ENV: dict[str, str | None] = {}
_TEST_ENV = {
    "IBKR_FLEX_TOKEN": "test-token",
    "IBKR_FLEX_QUERY_ID": "123456",
    "IBKR_BRIDGE_WS_URL": "ws://bridge:5000/ibkr/ws/events",
    "IBKR_BRIDGE_API_TOKEN": "bridge-token",
    "IBKR_LISTENER_ENABLED": "true",
    "IBKR_LISTENER_EXEC_EVENTS_ENABLED": "false",
    "IBKR_LISTENER_DEBOUNCE_MS": "0",
    "IBKR_POLL_INTERVAL": "300",
}


def setUpModule() -> None:
    for key, val in _TEST_ENV.items():
        _ORIG_ENV[key] = os.environ.get(key)
        os.environ[key] = val


def tearDownModule() -> None:
    for key, orig in _ORIG_ENV.items():
        if orig is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = orig


# ── Test envelope factory ────────────────────────────────────────────

_DEFAULT_CONTRACT = WsContract(
    secType="STK", conId=265598, symbol="AAPL",
    lastTradeDateOrContractMonth="", strike=0.0, right="",
    multiplier="", exchange="SMART", primaryExchange="NASDAQ",
    currency="USD", localSymbol="AAPL", tradingClass="AAPL",
    includeExpired=False, secIdType="", secId="", description="",
    issuerId="", comboLegsDescrip="",
)

_DEFAULT_EXECUTION = WsExecution(
    execId="0001", time="20260411-10:30:00", acctNumber="UXXXXXXX",
    exchange="ISLAND", side="BOT", shares=100.0, price=150.25,
    permId=12345, clientId=1, orderId=42, liquidation=0,
    cumQty=100.0, avgPrice=150.25, orderRef="", evRule="",
    evMultiplier=0.0, modelCode="", lastLiquidity=0,
    pendingPriceRevision=False,
)

_DEFAULT_COMMISSION = WsCommissionReport(
    execId="0001", commission=1.05, currency="USD",
    realizedPNL=0.0, yield_=0.0, yieldRedemptionDate=0,
)


def _make_envelope(
    event_type: str = "commissionReportEvent",
    seq: int = 1,
    exec_id: str = "0001",
    side: str = "BOT",
    has_fill: bool = True,
) -> WsEnvelope:
    fill: WsFill | None = None
    if has_fill:
        fill = WsFill(
            contract=_DEFAULT_CONTRACT,
            execution=_DEFAULT_EXECUTION.model_copy(
                update={"execId": exec_id, "side": side},
            ),
            commissionReport=_DEFAULT_COMMISSION.model_copy(
                update={"execId": exec_id},
            ),
            time="20260411-10:30:00",
        )
    return WsEnvelope(
        type=cast(WsEventType, event_type),
        seq=seq,
        timestamp="2026-04-11T10:30:00+00:00",
        fill=fill,
    )


# ── Env var getter tests ────────────────────────────────────────────


class TestEnvVarGetters(unittest.TestCase):
    """Test IBKR-specific env var getters."""

    def test_flex_token(self) -> None:
        self.assertEqual(_get_flex_token(), "test-token")

    def test_flex_token_missing_returns_none(self) -> None:
        with patch.dict(os.environ, {"IBKR_FLEX_TOKEN": ""}):
            self.assertIsNone(_get_flex_token())

    def test_flex_query_id(self) -> None:
        self.assertEqual(_get_flex_query_id(), "123456")

    def test_poll_interval_ibkr_specific(self) -> None:
        self.assertEqual(get_poll_interval("ibkr"), 300)

    def test_poll_interval_falls_back_to_generic(self) -> None:
        with patch.dict(os.environ, {"POLL_INTERVAL": "120"}, clear=False):
            os.environ.pop("IBKR_POLL_INTERVAL", None)
            self.assertEqual(get_poll_interval("ibkr"), 120)

    def test_bridge_ws_url(self) -> None:
        self.assertEqual(
            _get_bridge_ws_url(), "ws://bridge:5000/ibkr/ws/events",
        )

    def test_bridge_api_token(self) -> None:
        self.assertEqual(_get_bridge_api_token(), "bridge-token")

    def test_listener_enabled(self) -> None:
        self.assertTrue(is_listener_enabled("ibkr"))

    def test_listener_disabled(self) -> None:
        with patch.dict(os.environ, {"IBKR_LISTENER_ENABLED": "false"}):
            self.assertFalse(is_listener_enabled("ibkr"))

    def test_exec_events_disabled(self) -> None:
        self.assertFalse(_is_exec_events_enabled())

    def test_exec_events_enabled(self) -> None:
        with patch.dict(os.environ, {"IBKR_LISTENER_EXEC_EVENTS_ENABLED": "true"}):
            self.assertTrue(_is_exec_events_enabled())

    def test_debounce_ms(self) -> None:
        self.assertEqual(get_debounce_ms("ibkr"), 0)

    def test_debounce_ms_invalid_relay_var_raises_with_var_name(self) -> None:
        with patch.dict(os.environ, {"IBKR_LISTENER_DEBOUNCE_MS": "abc"}), \
             self.assertRaises(SystemExit) as cm:
            get_debounce_ms("ibkr")
        self.assertIn("IBKR_LISTENER_DEBOUNCE_MS", str(cm.exception))
        self.assertIn("abc", str(cm.exception))

    def test_debounce_ms_invalid_fallback_var_raises_with_var_name(self) -> None:
        with patch.dict(os.environ, {
            "IBKR_LISTENER_DEBOUNCE_MS": "",
            "LISTENER_DEBOUNCE_MS": "xyz",
        }), self.assertRaises(SystemExit) as cm:
            get_debounce_ms("ibkr")
        self.assertIn("LISTENER_DEBOUNCE_MS", str(cm.exception))
        self.assertIn("xyz", str(cm.exception))

    def test_debounce_ms_negative_raises_with_var_name(self) -> None:
        with patch.dict(os.environ, {"IBKR_LISTENER_DEBOUNCE_MS": "-5"}), \
             self.assertRaises(SystemExit) as cm:
            get_debounce_ms("ibkr")
        self.assertIn("IBKR_LISTENER_DEBOUNCE_MS", str(cm.exception))

    # ── IBKR_ACCOUNT_TIMEZONE ──

    def test_account_timezone_defaults_to_utc(self) -> None:
        with patch.dict(os.environ, {"IBKR_ACCOUNT_TIMEZONE": ""}):
            tz = _get_account_timezone()
        self.assertEqual(tz.key, "UTC")

    def test_account_timezone_valid_iana(self) -> None:
        with patch.dict(os.environ, {"IBKR_ACCOUNT_TIMEZONE": "America/New_York"}):
            tz = _get_account_timezone()
        self.assertEqual(tz.key, "America/New_York")

    def test_account_timezone_invalid_raises_system_exit(self) -> None:
        with patch.dict(os.environ, {"IBKR_ACCOUNT_TIMEZONE": "Not/A_Zone"}), \
             self.assertRaises(SystemExit) as cm:
            _get_account_timezone()
        self.assertIn("IBKR_ACCOUNT_TIMEZONE", str(cm.exception))
        self.assertIn("Not/A_Zone", str(cm.exception))

    # ── IBKR_FLEX_LOOKBACK_DAYS ──

    def test_flex_lookback_days_unset_returns_none(self) -> None:
        with patch.dict(os.environ, {"IBKR_FLEX_LOOKBACK_DAYS": ""}):
            self.assertIsNone(_get_flex_lookback_days())

    def test_flex_lookback_days_valid(self) -> None:
        with patch.dict(os.environ, {"IBKR_FLEX_LOOKBACK_DAYS": "40"}):
            self.assertEqual(_get_flex_lookback_days(), 40)

    def test_flex_lookback_days_non_integer_raises(self) -> None:
        with patch.dict(os.environ, {"IBKR_FLEX_LOOKBACK_DAYS": "abc"}), \
             self.assertRaises(SystemExit) as cm:
            _get_flex_lookback_days()
        self.assertIn("IBKR_FLEX_LOOKBACK_DAYS", str(cm.exception))
        self.assertIn("abc", str(cm.exception))

    def test_flex_lookback_days_zero_raises(self) -> None:
        with patch.dict(os.environ, {"IBKR_FLEX_LOOKBACK_DAYS": "0"}), \
             self.assertRaises(SystemExit):
            _get_flex_lookback_days()

    def test_flex_lookback_days_above_cap_raises(self) -> None:
        with patch.dict(os.environ, {"IBKR_FLEX_LOOKBACK_DAYS": "366"}), \
             self.assertRaises(SystemExit) as cm:
            _get_flex_lookback_days()
        self.assertIn("365", str(cm.exception))


# ── Event filter tests ───────────────────────────────────────────────


class TestEventFilter(unittest.TestCase):
    """Test IBKR event_filter callback."""

    def test_connected_filtered(self) -> None:
        self.assertFalse(_event_filter({"type": "connected"}))

    def test_disconnected_filtered(self) -> None:
        self.assertFalse(_event_filter({"type": "disconnected"}))

    def test_unknown_type_filtered(self) -> None:
        self.assertFalse(_event_filter({"type": "unknownEvent"}))

    def test_exec_event_passes(self) -> None:
        self.assertTrue(_event_filter({"type": "execDetailsEvent"}))

    def test_commission_event_passes(self) -> None:
        self.assertTrue(_event_filter({"type": "commissionReportEvent"}))


# ── Map fill tests ───────────────────────────────────────────────────


class TestMapFill(unittest.TestCase):
    """Test WsEnvelope → Fill mapping."""

    def test_bot_maps_to_buy(self) -> None:
        envelope = _make_envelope(side="BOT")
        fill = _map_fill(envelope, _TEST_TZ)
        assert fill is not None
        self.assertEqual(fill.side, BuySell.BUY)

    def test_sld_maps_to_sell(self) -> None:
        envelope = _make_envelope(side="SLD")
        fill = _map_fill(envelope, _TEST_TZ)
        assert fill is not None
        self.assertEqual(fill.side, BuySell.SELL)

    def test_unknown_side_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown execution side"):
            _map_fill(_make_envelope(side="UNKNOWN"), _TEST_TZ)

    def test_no_fill_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "has no fill data"):
            _map_fill(_make_envelope(has_fill=False), _TEST_TZ)

    def test_empty_exec_id_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Empty execId"):
            _map_fill(_make_envelope(exec_id=""), _TEST_TZ)

    def test_fee_is_positive(self) -> None:
        fill = _map_fill(_make_envelope(), _TEST_TZ)
        assert fill is not None
        self.assertGreater(fill.fee, 0)

    def test_timestamp_is_canonical_utc(self) -> None:
        """The fixture's 20260411-10:30:00 (UTC) should pass through unchanged."""
        fill = _map_fill(_make_envelope(), _TEST_TZ)
        assert fill is not None
        self.assertEqual(fill.timestamp, "2026-04-11T10:30:00")

    def test_timestamp_converted_from_account_tz(self) -> None:
        """Same wall-clock, different assumed tz → different UTC output."""
        ny = ZoneInfo("America/New_York")
        fill = _map_fill(_make_envelope(), ny)
        assert fill is not None
        # 10:30 NY in April (EDT, -04:00) → UTC 14:30
        self.assertEqual(fill.timestamp, "2026-04-11T14:30:00")

    def test_bad_timestamp_raises(self) -> None:
        bad_envelope = _make_envelope()
        assert bad_envelope.fill is not None
        bad_envelope.fill.execution.time = "not-a-timestamp"
        with self.assertRaisesRegex(ValueError, "Bad execution time"):
            _map_fill(bad_envelope, _TEST_TZ)

    def test_equity_fill_has_no_option(self) -> None:
        # Default fixture is secType="STK" with symbol="AAPL" — Fill.option
        # only carries meaning for derivatives, so equities must be None.
        fill = _map_fill(_make_envelope(), _TEST_TZ)
        self.assertEqual(fill.assetClass, "equity")
        self.assertIsNone(fill.option)

    def test_equity_fill_uses_contract_symbol(self) -> None:
        # Sanity check the equity path is unchanged: Fill.symbol comes from
        # contract.symbol (not localSymbol — for stocks the two are usually
        # equal, but the code paths are now distinct).
        fill = _map_fill(_make_envelope(), _TEST_TZ)
        self.assertEqual(fill.symbol, "AAPL")  # _DEFAULT_CONTRACT.symbol


def _option_contract(**overrides: Any) -> WsContract:
    """Build a WsContract representing the AVGO  260508C00375000 call.

    Centralised so individual TestOptionMapFill cases can mutate one
    field at a time and exercise the validation branches.
    """
    base = _DEFAULT_CONTRACT.model_copy(update={
        "secType": "OPT",
        "symbol": "AVGO",
        "localSymbol": "AVGO  260508C00375000",
        "strike": 375.0,
        "right": "C",
        "lastTradeDateOrContractMonth": "20260508",
        "multiplier": "100",
    })
    return base.model_copy(update=overrides) if overrides else base


def _envelope_with_contract(contract: WsContract) -> WsEnvelope:
    envelope = _make_envelope()
    assert envelope.fill is not None
    envelope.fill.contract = contract
    return envelope


class TestOptionMapFill(unittest.TestCase):
    """WS path builds a full :class:`OptionContract` from ib_async fields.

    ib_async splits option identifiers across two ``Contract`` fields:
    ``symbol`` is the underlying ticker (e.g. ``"AVGO"``) and
    ``localSymbol`` is the OCC option ticker (e.g.
    ``"AVGO  260508C00375000"``).  The relay surfaces ``localSymbol`` as
    ``Fill.symbol`` with spaces stripped for URL-friendliness (e.g.
    ``"AVGO260508C00375000"``) and packs the option metadata — including
    the underlying — into ``Fill.option``.
    """

    # ── happy path ───────────────────────────────────────────────────

    def test_option_fill_uses_local_symbol_for_symbol(self) -> None:
        fill = _map_fill(_envelope_with_contract(_option_contract()), _TEST_TZ)
        self.assertEqual(fill.symbol, "AVGO260508C00375000")

    def test_option_fill_has_full_option_contract(self) -> None:
        fill = _map_fill(_envelope_with_contract(_option_contract()), _TEST_TZ)
        self.assertEqual(fill.assetClass, "option")
        opt = fill.option
        assert opt is not None
        self.assertEqual(opt.rootSymbol, "AVGO")
        self.assertEqual(opt.strike, 375.0)
        self.assertEqual(opt.expiryDate, "2026-05-08")
        self.assertEqual(opt.type, "call")

    def test_right_p_maps_to_put(self) -> None:
        fill = _map_fill(
            _envelope_with_contract(_option_contract(right="P")), _TEST_TZ,
        )
        opt = fill.option
        assert opt is not None
        self.assertEqual(opt.type, "put")

    def test_right_call_spelled_out_maps_to_call(self) -> None:
        # ib_async docstring lists "CALL" / "PUT" as valid alternative forms
        # alongside "C"/"P".  Both must be accepted.
        fill = _map_fill(
            _envelope_with_contract(_option_contract(right="CALL")), _TEST_TZ,
        )
        opt = fill.option
        assert opt is not None
        self.assertEqual(opt.type, "call")

    # ── validation: failures raise ValueError ────────────────────────

    def test_empty_local_symbol_raises(self) -> None:
        # An option fill with no localSymbol can't be uniquely identified —
        # every option on the same underlying would collapse to one symbol.
        with self.assertRaisesRegex(ValueError, "Empty localSymbol for option"):
            _map_fill(
                _envelope_with_contract(_option_contract(localSymbol="")),
                _TEST_TZ,
            )

    def test_empty_underlying_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Empty Contract.symbol on option"):
            _map_fill(
                _envelope_with_contract(_option_contract(symbol="")),
                _TEST_TZ,
            )

    def test_zero_strike_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Non-positive Contract.strike"):
            _map_fill(
                _envelope_with_contract(_option_contract(strike=0.0)),
                _TEST_TZ,
            )

    def test_empty_expiry_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Empty lastTradeDateOrContractMonth"):
            _map_fill(
                _envelope_with_contract(
                    _option_contract(lastTradeDateOrContractMonth=""),
                ),
                _TEST_TZ,
            )

    def test_bad_expiry_format_raises(self) -> None:
        # ``flex_date_to_iso`` accepts both YYYYMMDD and ISO YYYY-MM-DD as a
        # defensive forwarding measure — use a value that matches neither.
        with self.assertRaisesRegex(ValueError, "Bad lastTradeDateOrContractMonth"):
            _map_fill(
                _envelope_with_contract(
                    _option_contract(lastTradeDateOrContractMonth="20261308"),
                ),
                _TEST_TZ,
            )

    def test_unknown_right_raises(self) -> None:
        # Financial enum — never assume a default for option type.
        with self.assertRaisesRegex(ValueError, "Unknown Contract.right"):
            _map_fill(
                _envelope_with_contract(_option_contract(right="X")),
                _TEST_TZ,
            )


# ── on_message dispatch tests ───────────────────────────────────────


class TestOnMessage(unittest.IsolatedAsyncioTestCase):
    """Test the adapter's on_message callback dispatch logic."""

    async def test_commission_returns_fill_with_mark(self) -> None:
        handler = _on_message_factory(exec_events_enabled=False, tz=_TEST_TZ)
        envelope = _make_envelope(event_type="commissionReportEvent")
        data: dict[str, Any] = envelope.model_dump()

        results = await handler(data)

        self.assertEqual(len(results), 1)
        self.assertIsNotNone(results[0].fill)
        self.assertTrue(results[0].mark)
        if results[0].fill is None:
            raise RuntimeError("Expected fill to be set")
        self.assertEqual(results[0].fill.execId, "0001")

    async def test_exec_event_returns_fill_without_mark_when_enabled(self) -> None:
        handler = _on_message_factory(exec_events_enabled=True, tz=_TEST_TZ)
        envelope = _make_envelope(event_type="execDetailsEvent")
        data: dict[str, Any] = envelope.model_dump()

        results = await handler(data)

        self.assertEqual(len(results), 1)
        self.assertIsNotNone(results[0].fill)
        self.assertFalse(results[0].mark)

    async def test_exec_event_skipped_when_disabled(self) -> None:
        handler = _on_message_factory(exec_events_enabled=False, tz=_TEST_TZ)
        envelope = _make_envelope(event_type="execDetailsEvent")
        data: dict[str, Any] = envelope.model_dump()

        results = await handler(data)

        self.assertEqual(results, [])

    async def test_invalid_envelope_returns_error_result(self) -> None:
        handler = _on_message_factory(exec_events_enabled=True, tz=_TEST_TZ)
        data: dict[str, Any] = {"type": "commissionReportEvent", "bad": "data"}

        results = await handler(data)

        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0].fill)
        self.assertIsNotNone(results[0].error)
        assert results[0].error is not None
        self.assertIn("commissionReportEvent", results[0].error)


# ── Poller config tests ──────────────────────────────────────────────


class TestBuildPollerConfigs(unittest.TestCase):
    """Test poller config construction from env vars."""

    def test_single_poller(self) -> None:
        configs = _build_poller_configs(_TEST_TZ)
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].interval, 300)

    def test_dual_pollers(self) -> None:
        with patch.dict(os.environ, {
            "IBKR_FLEX_TOKEN_2": "tok2",
            "IBKR_FLEX_QUERY_ID_2": "789",
        }):
            configs = _build_poller_configs(_TEST_TZ)
            self.assertEqual(len(configs), 2)

    def test_second_poller_falls_back_to_primary_token(self) -> None:
        """Only IBKR_FLEX_QUERY_ID_2 set → uses primary IBKR_FLEX_TOKEN."""
        with patch.dict(os.environ, {"IBKR_FLEX_QUERY_ID_2": "789"}):
            configs = _build_poller_configs(_TEST_TZ)
            self.assertEqual(len(configs), 2)

    def test_second_poller_skipped_without_query_id(self) -> None:
        with patch.dict(os.environ, {"IBKR_FLEX_TOKEN_2": "tok2"}):
            # Missing IBKR_FLEX_QUERY_ID_2 → no second poller
            configs = _build_poller_configs(_TEST_TZ)
            self.assertEqual(len(configs), 1)

    def test_second_poller_no_token_at_all_raises(self) -> None:
        """IBKR_FLEX_QUERY_ID_2 set but no token anywhere → SystemExit."""
        env = {
            "IBKR_FLEX_TOKEN": "",
            "IBKR_FLEX_QUERY_ID": "",
            "IBKR_FLEX_QUERY_ID_2": "789",
        }
        with patch.dict(os.environ, env), self.assertRaises(SystemExit) as cm:
            _build_poller_configs(_TEST_TZ)
        self.assertIn("IBKR_FLEX_TOKEN", str(cm.exception))

    def test_no_flex_creds_returns_empty(self) -> None:
        """Listener-only mode: no Flex credentials → empty list."""
        env = {
            "IBKR_FLEX_TOKEN": "",
            "IBKR_FLEX_QUERY_ID": "",
        }
        with patch.dict(os.environ, env):
            configs = _build_poller_configs(_TEST_TZ)
        self.assertEqual(configs, [])

    def test_poller_disabled_returns_empty(self) -> None:
        """IBKR_POLLER_ENABLED=false → empty list even with creds."""
        with patch.dict(os.environ, {"IBKR_POLLER_ENABLED": "false"}):
            configs = _build_poller_configs(_TEST_TZ)
        self.assertEqual(configs, [])

    def test_partial_config_raises(self) -> None:
        """Token without query ID → SystemExit."""
        env = {"IBKR_FLEX_TOKEN": "tok", "IBKR_FLEX_QUERY_ID": ""}
        with patch.dict(os.environ, env), self.assertRaises(SystemExit) as cm:
            _build_poller_configs(_TEST_TZ)
        self.assertIn("IBKR_FLEX_QUERY_ID", str(cm.exception))

    def test_partial_config_reverse_raises(self) -> None:
        """Query ID without token → SystemExit."""
        env = {"IBKR_FLEX_TOKEN": "", "IBKR_FLEX_QUERY_ID": "123"}
        with patch.dict(os.environ, env), self.assertRaises(SystemExit) as cm:
            _build_poller_configs(_TEST_TZ)
        self.assertIn("IBKR_FLEX_TOKEN", str(cm.exception))


# ── _build_connect / last_seq tracking tests ────────────────────────


def _make_mock_ws(messages: list[str]) -> Any:
    """Create a mock WS whose receive() yields *messages* then CLOSED."""
    msg_iter = iter(messages)

    async def _receive() -> Any:
        try:
            text = next(msg_iter)
        except StopIteration:
            from aiohttp import WSMessage, WSMsgType
            return WSMessage(WSMsgType.CLOSED, None, None)
        from aiohttp import WSMessage, WSMsgType
        return WSMessage(WSMsgType.TEXT, text, None)

    ws = AsyncMock()
    ws.receive = _receive
    return ws


class TestBuildConnect(unittest.IsolatedAsyncioTestCase):
    """Test that _build_connect tracks last_seq across reconnects."""

    async def test_last_seq_appended_on_reconnect(self) -> None:
        """After receiving seq=5, the next connect should add last_seq=5."""
        connect = _build_connect("ws://bridge/ws", "tok")
        session = AsyncMock()

        # First connect — returns a WS that yields a message with seq=5.
        ws1 = _make_mock_ws([json.dumps({"type": "connected", "seq": 5})])
        session.ws_connect = AsyncMock(return_value=ws1)

        ws_result = await connect(session)
        # URL should be unchanged on first connect.
        session.ws_connect.assert_called_once()
        called_url = session.ws_connect.call_args[0][0]
        self.assertEqual(called_url, "ws://bridge/ws")

        # Consume the message so _tracking_receive updates last_seq.
        await ws_result.receive()

        # Second connect — should append last_seq=5.
        ws2 = _make_mock_ws([])
        session.ws_connect = AsyncMock(return_value=ws2)
        await connect(session)
        called_url = session.ws_connect.call_args[0][0]
        self.assertEqual(called_url, "ws://bridge/ws?last_seq=5")

    async def test_last_seq_uses_ampersand_when_url_has_query(self) -> None:
        connect = _build_connect("ws://bridge/ws?foo=bar", "tok")
        session = AsyncMock()

        ws1 = _make_mock_ws([json.dumps({"seq": 3})])
        session.ws_connect = AsyncMock(return_value=ws1)
        ws_result = await connect(session)
        await ws_result.receive()

        ws2 = _make_mock_ws([])
        session.ws_connect = AsyncMock(return_value=ws2)
        await connect(session)
        called_url = session.ws_connect.call_args[0][0]
        self.assertEqual(called_url, "ws://bridge/ws?foo=bar&last_seq=3")

    async def test_non_int_seq_ignored(self) -> None:
        connect = _build_connect("ws://bridge/ws", "tok")
        session = AsyncMock()

        ws1 = _make_mock_ws([json.dumps({"seq": "not-a-number"})])
        session.ws_connect = AsyncMock(return_value=ws1)
        ws_result = await connect(session)
        await ws_result.receive()

        # Second connect — last_seq should still be 0, so no param appended.
        ws2 = _make_mock_ws([])
        session.ws_connect = AsyncMock(return_value=ws2)
        await connect(session)
        called_url = session.ws_connect.call_args[0][0]
        self.assertEqual(called_url, "ws://bridge/ws")

    async def test_invalid_json_ignored(self) -> None:
        connect = _build_connect("ws://bridge/ws", "tok")
        session = AsyncMock()

        ws1 = _make_mock_ws(["not json at all"])
        session.ws_connect = AsyncMock(return_value=ws1)
        ws_result = await connect(session)
        await ws_result.receive()

        ws2 = _make_mock_ws([])
        session.ws_connect = AsyncMock(return_value=ws2)
        await connect(session)
        called_url = session.ws_connect.call_args[0][0]
        self.assertEqual(called_url, "ws://bridge/ws")

    async def test_auth_header_sent(self) -> None:
        connect = _build_connect("ws://bridge/ws", "my-secret")
        session = AsyncMock()

        ws1 = _make_mock_ws([])
        session.ws_connect = AsyncMock(return_value=ws1)
        await connect(session)

        _, kwargs = session.ws_connect.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer my-secret")


# ── build_relay integration test ─────────────────────────────────────


class TestBuildRelay(unittest.TestCase):
    """Test that build_relay wires everything together."""

    def test_relay_name_is_ibkr(self) -> None:
        relay = build_relay(notifiers=[])
        self.assertEqual(relay.name, "ibkr")

    def test_has_poller_and_listener(self) -> None:
        relay = build_relay(notifiers=[])
        self.assertGreaterEqual(len(relay.poller_configs), 1)
        self.assertIsNotNone(relay.listener_config)

    def test_listener_none_when_disabled(self) -> None:
        with patch.dict(os.environ, {"IBKR_LISTENER_ENABLED": "false"}):
            relay = build_relay(notifiers=[])
        self.assertIsNone(relay.listener_config)

    def test_no_poller_no_listener_raises(self) -> None:
        """Neither poller nor listener configured → SystemExit."""
        env = {
            "IBKR_POLLER_ENABLED": "false",
            "IBKR_LISTENER_ENABLED": "false",
        }
        with patch.dict(os.environ, env), self.assertRaises(SystemExit) as cm:
            build_relay(notifiers=[])
        self.assertIn("neither poller nor listener", str(cm.exception))

    def test_listener_only_mode(self) -> None:
        """Poller disabled + listener enabled → works, no poller configs."""
        env = {
            "IBKR_POLLER_ENABLED": "false",
            "IBKR_LISTENER_ENABLED": "true",
        }
        with patch.dict(os.environ, env):
            relay = build_relay(notifiers=[])
        self.assertEqual(relay.poller_configs, [])
        self.assertIsNotNone(relay.listener_config)
