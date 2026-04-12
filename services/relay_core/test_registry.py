"""Tests for the relay registry."""

import importlib
import os
import unittest
from collections.abc import Callable
from unittest.mock import MagicMock, patch

from relay_core import BrokerRelay
from relay_core.registry import (
    _VALID_RELAY_NAMES,
    _load_adapter,
    get_relay_names,
    load_relays,
)

# ── Env var setup ────────────────────────────────────────────────────

_ORIG_ENV: dict[str, str | None] = {}
_TEST_ENV = {
    "RELAYS": "ibkr",
    "IBKR_FLEX_TOKEN": "test-token",
    "IBKR_FLEX_QUERY_ID": "123456",
    "IBKR_BRIDGE_WS_URL": "ws://bridge:5000/ibkr/ws/events",
    "IBKR_BRIDGE_API_TOKEN": "bridge-token",
    "IBKR_LISTENER_ENABLED": "true",
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


# ── get_relay_names tests ────────────────────────────────────────────


class TestGetRelayNames(unittest.TestCase):
    """Test RELAYS env var parsing and validation."""

    def test_single_relay(self) -> None:
        with patch.dict(os.environ, {"RELAYS": "ibkr"}):
            self.assertEqual(get_relay_names(), ["ibkr"])

    def test_whitespace_trimmed(self) -> None:
        with patch.dict(os.environ, {"RELAYS": "  ibkr  "}):
            self.assertEqual(get_relay_names(), ["ibkr"])

    def test_case_normalized_to_lower(self) -> None:
        with patch.dict(os.environ, {"RELAYS": "IBKR"}):
            self.assertEqual(get_relay_names(), ["ibkr"])

    def test_empty_returns_empty(self) -> None:
        with patch.dict(os.environ, {"RELAYS": ""}):
            self.assertEqual(get_relay_names(), [])

    def test_unset_returns_empty(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RELAYS", None)
            self.assertEqual(get_relay_names(), [])

    def test_unknown_relay_raises(self) -> None:
        with patch.dict(os.environ, {"RELAYS": "unknown"}), \
             self.assertRaises(SystemExit) as ctx:
            get_relay_names()
        self.assertIn("unknown", str(ctx.exception))

    def test_skips_empty_segments(self) -> None:
        with patch.dict(os.environ, {"RELAYS": "ibkr,,"}):
            self.assertEqual(get_relay_names(), ["ibkr"])

    def test_only_commas_returns_empty(self) -> None:
        with patch.dict(os.environ, {"RELAYS": ",,,"}):
            self.assertEqual(get_relay_names(), [])


class TestValidRelayNames(unittest.TestCase):
    """Test that _VALID_RELAY_NAMES is derived from the Literal type."""

    def test_ibkr_is_valid(self) -> None:
        self.assertIn("ibkr", _VALID_RELAY_NAMES)


class TestAdapterContract(unittest.TestCase):
    """Verify every registered relay name has a valid adapter module.

    Catches missing ``build_relay`` at test time instead of runtime.
    """

    def test_all_adapters_export_build_relay(self) -> None:
        for name in sorted(_VALID_RELAY_NAMES):
            with self.subTest(relay=name):
                mod = importlib.import_module(f"relays.{name}")
                build_fn = getattr(mod, "build_relay", None)
                self.assertIsNotNone(
                    build_fn,
                    f"relays.{name} does not export build_relay()",
                )
                self.assertIsInstance(
                    build_fn, Callable,  # type: ignore[arg-type]
                    f"relays.{name}.build_relay is not callable",
                )


# ── _load_adapter tests ─────────────────────────────────────────────


class TestLoadAdapter(unittest.TestCase):
    """Test adapter loading via importlib."""

    def test_loads_ibkr_adapter(self) -> None:
        relay = _load_adapter("ibkr", notifiers=[])
        self.assertIsInstance(relay, BrokerRelay)
        self.assertEqual(relay.name, "ibkr")

    @patch("relay_core.registry.importlib.import_module", side_effect=ImportError("nope"))
    def test_import_error_raises_system_exit(self, _mock: MagicMock) -> None:
        with self.assertRaises(SystemExit) as ctx:
            _load_adapter("ibkr", notifiers=[])
        self.assertIn("Failed to import", str(ctx.exception))

    @patch("relay_core.registry.importlib.import_module")
    def test_missing_build_relay_raises(
        self, mock_import: MagicMock,
    ) -> None:
        mock_mod = MagicMock(spec=[])  # No build_relay attribute
        mock_import.return_value = mock_mod
        with self.assertRaises(SystemExit) as ctx:
            _load_adapter("ibkr", notifiers=[])
        self.assertIn("does not export build_relay", str(ctx.exception))


# ── load_relays integration test ─────────────────────────────────────


class TestLoadRelays(unittest.TestCase):
    """Test end-to-end relay loading."""

    @patch("relay_core.registry.load_notifiers", return_value=[])
    def test_loads_configured_relays(self, _mock: MagicMock) -> None:
        relays = load_relays()
        self.assertEqual(len(relays), 1)
        self.assertEqual(relays[0].name, "ibkr")

    @patch("relay_core.registry.load_notifiers", return_value=[])
    def test_relay_has_poller_configs(self, _mock: MagicMock) -> None:
        relays = load_relays()
        self.assertGreaterEqual(len(relays[0].poller_configs), 1)

    @patch("relay_core.registry.load_notifiers", return_value=[])
    def test_relay_has_listener_config(self, _mock: MagicMock) -> None:
        relays = load_relays()
        self.assertIsNotNone(relays[0].listener_config)
