"""Tests for the IBKR relay adapter."""

import os
import unittest
from typing import Any, cast
from unittest.mock import patch

from relay_core import get_debounce_ms, get_poll_interval, is_listener_enabled
from relays.ibkr import (
    _build_poller_configs,
    _event_filter,
    _get_bridge_api_token,
    _get_bridge_ws_url,
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

    def test_bridge_ws_url_falls_back_to_legacy(self) -> None:
        with patch.dict(
            os.environ, {"BRIDGE_WS_URL": "ws://legacy:5000"}, clear=False,
        ):
            os.environ.pop("IBKR_BRIDGE_WS_URL", None)
            self.assertEqual(_get_bridge_ws_url(), "ws://legacy:5000")

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

    def test_debounce_ms_invalid_raises(self) -> None:
        with patch.dict(os.environ, {"IBKR_LISTENER_DEBOUNCE_MS": "abc"}), \
             self.assertRaises(SystemExit):
            get_debounce_ms("ibkr")


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
        fill = _map_fill(envelope)
        assert fill is not None
        self.assertEqual(fill.side, BuySell.BUY)

    def test_sld_maps_to_sell(self) -> None:
        envelope = _make_envelope(side="SLD")
        fill = _map_fill(envelope)
        assert fill is not None
        self.assertEqual(fill.side, BuySell.SELL)

    def test_unknown_side_returns_none(self) -> None:
        self.assertIsNone(_map_fill(_make_envelope(side="UNKNOWN")))

    def test_no_fill_returns_none(self) -> None:
        self.assertIsNone(_map_fill(_make_envelope(has_fill=False)))

    def test_empty_exec_id_returns_none(self) -> None:
        self.assertIsNone(_map_fill(_make_envelope(exec_id="")))

    def test_fee_is_positive(self) -> None:
        fill = _map_fill(_make_envelope())
        assert fill is not None
        self.assertGreater(fill.fee, 0)


# ── on_message dispatch tests ───────────────────────────────────────


class TestOnMessage(unittest.IsolatedAsyncioTestCase):
    """Test the adapter's on_message callback dispatch logic."""

    async def test_commission_returns_fill_with_mark(self) -> None:
        handler = _on_message_factory(exec_events_enabled=False)
        envelope = _make_envelope(event_type="commissionReportEvent")
        data: dict[str, Any] = envelope.model_dump()

        result = await handler(data)

        self.assertIsNotNone(result.fill)
        self.assertTrue(result.mark)
        if result.fill is None:
            raise RuntimeError("Expected fill to be set")
        self.assertEqual(result.fill.execId, "0001")

    async def test_exec_event_returns_fill_without_mark_when_enabled(self) -> None:
        handler = _on_message_factory(exec_events_enabled=True)
        envelope = _make_envelope(event_type="execDetailsEvent")
        data: dict[str, Any] = envelope.model_dump()

        result = await handler(data)

        self.assertIsNotNone(result.fill)
        self.assertFalse(result.mark)

    async def test_exec_event_skipped_when_disabled(self) -> None:
        handler = _on_message_factory(exec_events_enabled=False)
        envelope = _make_envelope(event_type="execDetailsEvent")
        data: dict[str, Any] = envelope.model_dump()

        result = await handler(data)

        self.assertIsNone(result.fill)

    async def test_invalid_envelope_skipped(self) -> None:
        handler = _on_message_factory(exec_events_enabled=True)
        data: dict[str, Any] = {"type": "commissionReportEvent", "bad": "data"}

        result = await handler(data)

        self.assertIsNone(result.fill)


# ── Poller config tests ──────────────────────────────────────────────


class TestBuildPollerConfigs(unittest.TestCase):
    """Test poller config construction from env vars."""

    def test_single_poller(self) -> None:
        configs = _build_poller_configs()
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].interval, 300)

    def test_dual_pollers(self) -> None:
        with patch.dict(os.environ, {
            "IBKR_FLEX_TOKEN_2": "tok2",
            "IBKR_FLEX_QUERY_ID_2": "789",
        }):
            configs = _build_poller_configs()
            self.assertEqual(len(configs), 2)

    def test_second_poller_skipped_if_incomplete(self) -> None:
        with patch.dict(os.environ, {"IBKR_FLEX_TOKEN_2": "tok2"}):
            # Missing IBKR_FLEX_QUERY_ID_2
            configs = _build_poller_configs()
            self.assertEqual(len(configs), 1)

    def test_no_flex_creds_returns_empty(self) -> None:
        """Listener-only mode: no Flex credentials → empty list."""
        env = {
            "IBKR_FLEX_TOKEN": "",
            "IBKR_FLEX_QUERY_ID": "",
        }
        with patch.dict(os.environ, env):
            configs = _build_poller_configs()
        self.assertEqual(configs, [])

    def test_poller_disabled_returns_empty(self) -> None:
        """IBKR_POLLER_ENABLED=false → empty list even with creds."""
        with patch.dict(os.environ, {"IBKR_POLLER_ENABLED": "false"}):
            configs = _build_poller_configs()
        self.assertEqual(configs, [])

    def test_partial_config_raises(self) -> None:
        """Token without query ID → SystemExit."""
        env = {"IBKR_FLEX_TOKEN": "tok", "IBKR_FLEX_QUERY_ID": ""}
        with patch.dict(os.environ, env), self.assertRaises(SystemExit) as cm:
            _build_poller_configs()
        self.assertIn("IBKR_FLEX_QUERY_ID", str(cm.exception))

    def test_partial_config_reverse_raises(self) -> None:
        """Query ID without token → SystemExit."""
        env = {"IBKR_FLEX_TOKEN": "", "IBKR_FLEX_QUERY_ID": "123"}
        with patch.dict(os.environ, env), self.assertRaises(SystemExit) as cm:
            _build_poller_configs()
        self.assertIn("IBKR_FLEX_TOKEN", str(cm.exception))


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
