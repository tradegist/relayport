"""Tests for the Kraken relay adapter."""

import json
import os
import time
import unittest
from typing import Any
from unittest.mock import patch

from relay_core import (
    OnMessageResult,
    get_debounce_ms,
    get_poll_interval,
    is_listener_enabled,
)
from relays.kraken import (
    _build_listener_config,
    _build_parse,
    _build_poller_configs,
    _event_filter,
    _get_api_key,
    _get_api_secret,
    _on_message,
    _parse_rest_trade,
    build_relay,
)
from shared import BuySell

from .kraken_types import KrakenRestTrade

# ── Env var setup ─────────────────────────────────────────────────────────────

_ORIG_ENV: dict[str, str | None] = {}
_TEST_ENV = {
    "KRAKEN_API_KEY": "test-api-key",
    # base64.b64encode(b"test-secret-32-bytes-padded!!!!!") → valid base64
    "KRAKEN_API_SECRET": "dGVzdC1zZWNyZXQ=",
    "KRAKEN_LISTENER_ENABLED": "true",
    "KRAKEN_POLL_INTERVAL": "60",
    "KRAKEN_LISTENER_DEBOUNCE_MS": "0",
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


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_rest_trade(**overrides: object) -> KrakenRestTrade:
    base: KrakenRestTrade = {
        "ordertxid": "ORD-001",
        "pair": "XBTUSD",
        "time": 1744447200.0,
        "type": "buy",
        "ordertype": "limit",
        "price": "65000.0",
        "vol": "0.1",
        "cost": "6500.0",
        "fee": "6.5",
    }
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


# ── Env var getter tests ──────────────────────────────────────────────────────


class TestEnvVarGetters(unittest.TestCase):
    """Test Kraken-specific env var getters."""

    def test_api_key(self) -> None:
        self.assertEqual(_get_api_key(), "test-api-key")

    def test_api_key_missing_returns_none(self) -> None:
        with patch.dict(os.environ, {"KRAKEN_API_KEY": ""}):
            self.assertIsNone(_get_api_key())

    def test_api_secret(self) -> None:
        self.assertEqual(_get_api_secret(), "dGVzdC1zZWNyZXQ=")

    def test_api_secret_missing_returns_none(self) -> None:
        with patch.dict(os.environ, {"KRAKEN_API_SECRET": ""}):
            self.assertIsNone(_get_api_secret())

    def test_poll_interval_kraken_specific(self) -> None:
        self.assertEqual(get_poll_interval("kraken"), 60)

    def test_poll_interval_falls_back_to_generic(self) -> None:
        with patch.dict(os.environ, {"POLL_INTERVAL": "120"}, clear=False):
            os.environ.pop("KRAKEN_POLL_INTERVAL", None)
            self.assertEqual(get_poll_interval("kraken"), 120)

    def test_listener_enabled(self) -> None:
        self.assertTrue(is_listener_enabled("kraken"))

    def test_listener_disabled(self) -> None:
        with patch.dict(os.environ, {"KRAKEN_LISTENER_ENABLED": "false"}):
            self.assertFalse(is_listener_enabled("kraken"))

    def test_debounce_ms(self) -> None:
        self.assertEqual(get_debounce_ms("kraken"), 0)

    def test_debounce_ms_invalid_raises_with_var_name(self) -> None:
        with patch.dict(os.environ, {"KRAKEN_LISTENER_DEBOUNCE_MS": "abc"}), \
             self.assertRaises(SystemExit) as cm:
            get_debounce_ms("kraken")
        self.assertIn("KRAKEN_LISTENER_DEBOUNCE_MS", str(cm.exception))
        self.assertIn("abc", str(cm.exception))


# ── Event filter tests ────────────────────────────────────────────────────────


class TestEventFilter(unittest.TestCase):
    """Test Kraken event_filter callback."""

    def test_heartbeat_filtered(self) -> None:
        self.assertFalse(_event_filter({"channel": "heartbeat"}))

    def test_subscribe_ack_filtered(self) -> None:
        self.assertFalse(_event_filter({"method": "subscribe", "success": True}))

    def test_unsubscribe_ack_filtered(self) -> None:
        self.assertFalse(_event_filter({"method": "unsubscribe", "success": True}))

    def test_executions_channel_passes(self) -> None:
        self.assertTrue(_event_filter({"channel": "executions"}))

    def test_other_channel_filtered(self) -> None:
        self.assertFalse(_event_filter({"channel": "ticker"}))

    def test_no_channel_filtered(self) -> None:
        self.assertFalse(_event_filter({}))


# ── REST trade parser tests ───────────────────────────────────────────────────


class TestParseRestTrade(unittest.TestCase):
    """Test _parse_rest_trade: KrakenRestTrade → Fill."""

    def test_fields_mapped_correctly(self) -> None:
        fill = _parse_rest_trade("TXID-001", _make_rest_trade())
        self.assertEqual(fill.execId, "TXID-001")
        self.assertEqual(fill.orderId, "ORD-001")
        self.assertEqual(fill.symbol, "XBTUSD")
        self.assertEqual(fill.assetClass, "crypto")
        self.assertEqual(fill.price, 65000.0)
        self.assertEqual(fill.volume, 0.1)
        self.assertEqual(fill.cost, 6500.0)
        self.assertEqual(fill.fee, 6.5)
        self.assertEqual(fill.source, "rest_poll")

    def test_buy_side(self) -> None:
        fill = _parse_rest_trade("T1", _make_rest_trade(type="buy"))
        self.assertEqual(fill.side, BuySell.BUY)

    def test_sell_side(self) -> None:
        fill = _parse_rest_trade("T1", _make_rest_trade(type="sell"))
        self.assertEqual(fill.side, BuySell.SELL)

    def test_invalid_side_raises(self) -> None:
        with self.assertRaises(ValueError):
            _parse_rest_trade("T1", _make_rest_trade(type="short"))

    def test_order_type_mapped(self) -> None:
        fill = _parse_rest_trade("T1", _make_rest_trade(ordertype="stop-loss"))
        self.assertEqual(fill.orderType, "stop")

    def test_unknown_order_type_is_none(self) -> None:
        fill = _parse_rest_trade("T1", _make_rest_trade(ordertype="algo"))
        self.assertIsNone(fill.orderType)

    def test_fee_is_absolute_value(self) -> None:
        fill = _parse_rest_trade("T1", _make_rest_trade(fee="-3.0"))
        self.assertEqual(fill.fee, 3.0)

    def test_timestamp_is_utc_iso_string(self) -> None:
        fill = _parse_rest_trade("T1", _make_rest_trade())
        expected = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1744447200.0))
        self.assertEqual(fill.timestamp, expected)

    def test_raw_contains_txid_and_trade_data(self) -> None:
        fill = _parse_rest_trade("TXID-001", _make_rest_trade())
        self.assertEqual(fill.raw["txid"], "TXID-001")
        self.assertEqual(fill.raw["pair"], "XBTUSD")


# ── _build_parse tests ────────────────────────────────────────────────────────


class TestBuildParse(unittest.TestCase):
    """Test the parse callable returned by _build_parse()."""

    def _parse(self, trades: dict[str, Any]) -> tuple[list[Any], list[str]]:
        result: tuple[list[Any], list[str]] = _build_parse()(json.dumps({"trades": trades}))
        return result

    def test_valid_trade_returns_fill(self) -> None:
        fills, errors = self._parse({"T1": _make_rest_trade()})
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].execId, "T1")
        self.assertEqual(errors, [])

    def test_empty_trades_returns_empty(self) -> None:
        fills, errors = self._parse({})
        self.assertEqual(fills, [])
        self.assertEqual(errors, [])

    def test_invalid_trade_appends_error(self) -> None:
        fills, errors = self._parse({"T1": _make_rest_trade(type="bad")})
        self.assertEqual(fills, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("T1", errors[0])

    def test_partial_success_mixed_trades(self) -> None:
        trades = {
            "GOOD": _make_rest_trade(),
            "BAD": _make_rest_trade(type="???"),
        }
        fills, errors = self._parse(trades)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].execId, "GOOD")
        self.assertEqual(len(errors), 1)
        self.assertIn("BAD", errors[0])

    def test_missing_trades_key_returns_empty(self) -> None:
        parse = _build_parse()
        fills, errors = parse(json.dumps({}))
        self.assertEqual(fills, [])
        self.assertEqual(errors, [])


# ── Poller config tests ───────────────────────────────────────────────────────


class TestBuildPollerConfigs(unittest.TestCase):
    """Test poller config construction from env vars."""

    def test_single_poller_with_credentials(self) -> None:
        configs = _build_poller_configs()
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].interval, 60)

    def test_no_credentials_returns_empty(self) -> None:
        env = {"KRAKEN_API_KEY": "", "KRAKEN_API_SECRET": ""}
        with patch.dict(os.environ, env):
            configs = _build_poller_configs()
        self.assertEqual(configs, [])

    def test_only_key_set_raises_missing_secret(self) -> None:
        env = {"KRAKEN_API_KEY": "key", "KRAKEN_API_SECRET": ""}
        with patch.dict(os.environ, env), self.assertRaises(SystemExit) as cm:
            _build_poller_configs()
        self.assertIn("KRAKEN_API_SECRET", str(cm.exception))

    def test_only_secret_set_raises_missing_key(self) -> None:
        env = {"KRAKEN_API_KEY": "", "KRAKEN_API_SECRET": "c2VjcmV0"}
        with patch.dict(os.environ, env), self.assertRaises(SystemExit) as cm:
            _build_poller_configs()
        self.assertIn("KRAKEN_API_KEY", str(cm.exception))

    def test_invalid_base64_secret_raises_at_config_time(self) -> None:
        env = {"KRAKEN_API_KEY": "key", "KRAKEN_API_SECRET": "not-valid-base64!!!"}
        with patch.dict(os.environ, env), self.assertRaises(SystemExit) as cm:
            _build_poller_configs()
        self.assertIn("base64", str(cm.exception))

    def test_poller_disabled_returns_empty(self) -> None:
        with patch.dict(os.environ, {"KRAKEN_POLLER_ENABLED": "false"}):
            configs = _build_poller_configs()
        self.assertEqual(configs, [])


# ── Listener config tests ─────────────────────────────────────────────────────


class TestBuildListenerConfig(unittest.TestCase):
    """Test listener config construction from env vars."""

    def test_listener_enabled_returns_config(self) -> None:
        config = _build_listener_config()
        self.assertIsNotNone(config)

    def test_listener_disabled_returns_none(self) -> None:
        with patch.dict(os.environ, {"KRAKEN_LISTENER_ENABLED": "false"}):
            config = _build_listener_config()
        self.assertIsNone(config)

    def test_listener_without_credentials_raises(self) -> None:
        env = {"KRAKEN_API_KEY": "", "KRAKEN_API_SECRET": ""}
        with patch.dict(os.environ, env), self.assertRaises(SystemExit) as cm:
            _build_listener_config()
        self.assertIn("KRAKEN_API_KEY", str(cm.exception))

    def test_invalid_base64_secret_raises_at_config_time(self) -> None:
        env = {"KRAKEN_API_KEY": "key", "KRAKEN_API_SECRET": "not-valid-base64!!!"}
        with patch.dict(os.environ, env), self.assertRaises(SystemExit) as cm:
            _build_listener_config()
        self.assertIn("base64", str(cm.exception))


# ── on_message tests ──────────────────────────────────────────────────────────


class TestOnMessage(unittest.IsolatedAsyncioTestCase):
    """Test the _on_message async handler."""

    async def test_valid_execution_returns_result_with_mark(self) -> None:
        msg = {
            "channel": "executions",
            "data": [{
                "exec_type": "trade",
                "exec_id": "EXEC-1",
                "order_id": "ORD-1",
                "symbol": "BTC/USD",
                "side": "buy",
                "order_type": "limit",
                "last_price": 65000.0,
                "last_qty": 0.1,
                "cost": 6500.0,
                "fees": [{"asset": "USD", "qty": 6.5}],
                "timestamp": "2026-04-12T10:00:00Z",
            }],
        }
        results = await _on_message(msg)
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], OnMessageResult)
        self.assertTrue(results[0].mark)
        self.assertIsNotNone(results[0].fill)
        fill = results[0].fill
        self.assertEqual(fill.execId, "EXEC-1")

    async def test_non_trade_exec_type_returns_empty(self) -> None:
        msg = {
            "channel": "executions",
            "data": [{"exec_type": "pending_new", "exec_id": "E1"}],
        }
        results = await _on_message(msg)
        self.assertEqual(results, [])

    async def test_parse_errors_return_empty_no_exception(self) -> None:
        msg = {
            "channel": "executions",
            "data": [{"exec_type": "trade", "exec_id": "E1", "side": "bad"}],
        }
        results = await _on_message(msg)
        self.assertEqual(results, [])

    async def test_empty_data_returns_empty(self) -> None:
        msg = {"channel": "executions", "data": []}
        results = await _on_message(msg)
        self.assertEqual(results, [])


# ── build_relay integration tests ─────────────────────────────────────────────


class TestBuildRelay(unittest.TestCase):
    """Test that build_relay wires everything together."""

    def test_relay_name_is_kraken(self) -> None:
        relay = build_relay(notifiers=[])
        self.assertEqual(relay.name, "kraken")

    def test_has_poller_and_listener(self) -> None:
        relay = build_relay(notifiers=[])
        self.assertGreaterEqual(len(relay.poller_configs), 1)
        self.assertIsNotNone(relay.listener_config)

    def test_listener_none_when_disabled(self) -> None:
        with patch.dict(os.environ, {"KRAKEN_LISTENER_ENABLED": "false"}):
            relay = build_relay(notifiers=[])
        self.assertIsNone(relay.listener_config)

    def test_no_poller_no_listener_raises(self) -> None:
        env = {
            "KRAKEN_POLLER_ENABLED": "false",
            "KRAKEN_LISTENER_ENABLED": "false",
        }
        with patch.dict(os.environ, env), self.assertRaises(SystemExit) as cm:
            build_relay(notifiers=[])
        self.assertIn("neither poller nor listener", str(cm.exception))

    def test_listener_only_mode(self) -> None:
        env = {
            "KRAKEN_POLLER_ENABLED": "false",
            "KRAKEN_LISTENER_ENABLED": "true",
        }
        with patch.dict(os.environ, env):
            relay = build_relay(notifiers=[])
        self.assertEqual(relay.poller_configs, [])
        self.assertIsNotNone(relay.listener_config)

    def test_poller_only_mode(self) -> None:
        with patch.dict(os.environ, {"KRAKEN_LISTENER_ENABLED": "false"}):
            relay = build_relay(notifiers=[])
        self.assertEqual(len(relay.poller_configs), 1)
        self.assertIsNone(relay.listener_config)
