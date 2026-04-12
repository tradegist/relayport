"""Tests for relay-agnostic env var getters."""

import os
import unittest
from typing import cast
from unittest.mock import patch

from relay_core import get_debounce_ms, get_poll_interval, is_listener_enabled, is_poller_enabled
from relay_core.env import get_env, get_env_int
from shared import RelayName

# Fake relay name to test generic prefix logic (not a real relay).
_FOO = cast(RelayName, "foo")


class TestGetPollInterval(unittest.TestCase):
    """Test get_poll_interval with relay-specific and generic fallback."""

    def test_relay_specific_var(self) -> None:
        with patch.dict(os.environ, {"FOO_POLL_INTERVAL": "120"}, clear=True):
            self.assertEqual(get_poll_interval(_FOO), 120)

    def test_falls_back_to_generic(self) -> None:
        with patch.dict(os.environ, {"POLL_INTERVAL": "300"}, clear=True):
            self.assertEqual(get_poll_interval(_FOO), 300)

    def test_relay_specific_takes_precedence(self) -> None:
        with patch.dict(os.environ, {
            "FOO_POLL_INTERVAL": "60",
            "POLL_INTERVAL": "300",
        }, clear=True):
            self.assertEqual(get_poll_interval(_FOO), 60)

    def test_default_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_poll_interval(_FOO), 600)

    def test_invalid_value_raises(self) -> None:
        with patch.dict(os.environ, {"FOO_POLL_INTERVAL": "abc"}, clear=True), \
             self.assertRaises(SystemExit):
            get_poll_interval(_FOO)

    def test_uppercases_relay_name(self) -> None:
        with patch.dict(os.environ, {"IBKR_POLL_INTERVAL": "90"}, clear=True):
            self.assertEqual(get_poll_interval("ibkr"), 90)


class TestIsPollerEnabled(unittest.TestCase):
    """Test is_poller_enabled with relay-specific and generic fallback."""

    def test_defaults_to_true(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(is_poller_enabled(_FOO))

    def test_relay_specific_false(self) -> None:
        with patch.dict(os.environ, {"FOO_POLLER_ENABLED": "false"}, clear=True):
            self.assertFalse(is_poller_enabled(_FOO))

    def test_relay_specific_true(self) -> None:
        with patch.dict(os.environ, {"FOO_POLLER_ENABLED": "true"}, clear=True):
            self.assertTrue(is_poller_enabled(_FOO))

    def test_falls_back_to_generic(self) -> None:
        with patch.dict(os.environ, {"POLLER_ENABLED": "false"}, clear=True):
            self.assertFalse(is_poller_enabled(_FOO))

    def test_relay_specific_takes_precedence(self) -> None:
        with patch.dict(os.environ, {
            "FOO_POLLER_ENABLED": "true",
            "POLLER_ENABLED": "false",
        }, clear=True):
            self.assertTrue(is_poller_enabled(_FOO))

    def test_zero_is_false(self) -> None:
        with patch.dict(os.environ, {"FOO_POLLER_ENABLED": "0"}, clear=True):
            self.assertFalse(is_poller_enabled(_FOO))

    def test_no_is_false(self) -> None:
        with patch.dict(os.environ, {"FOO_POLLER_ENABLED": "no"}, clear=True):
            self.assertFalse(is_poller_enabled(_FOO))


class TestIsListenerEnabled(unittest.TestCase):
    """Test is_listener_enabled with relay-specific and generic fallback."""

    def test_relay_specific_true(self) -> None:
        with patch.dict(os.environ, {"FOO_LISTENER_ENABLED": "true"}, clear=True):
            self.assertTrue(is_listener_enabled(_FOO))

    def test_relay_specific_false(self) -> None:
        with patch.dict(os.environ, {"FOO_LISTENER_ENABLED": "false"}, clear=True):
            self.assertFalse(is_listener_enabled(_FOO))

    def test_falls_back_to_generic(self) -> None:
        with patch.dict(os.environ, {"LISTENER_ENABLED": "true"}, clear=True):
            self.assertTrue(is_listener_enabled(_FOO))

    def test_relay_specific_takes_precedence(self) -> None:
        with patch.dict(os.environ, {
            "FOO_LISTENER_ENABLED": "false",
            "LISTENER_ENABLED": "true",
        }, clear=True):
            self.assertFalse(is_listener_enabled(_FOO))

    def test_unset_returns_false(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(is_listener_enabled(_FOO))

    def test_zero_is_false(self) -> None:
        with patch.dict(os.environ, {"FOO_LISTENER_ENABLED": "0"}, clear=True):
            self.assertFalse(is_listener_enabled(_FOO))

    def test_no_is_false(self) -> None:
        with patch.dict(os.environ, {"FOO_LISTENER_ENABLED": "no"}, clear=True):
            self.assertFalse(is_listener_enabled(_FOO))

    def test_yes_is_true(self) -> None:
        with patch.dict(os.environ, {"FOO_LISTENER_ENABLED": "yes"}, clear=True):
            self.assertTrue(is_listener_enabled(_FOO))


class TestGetDebounceMs(unittest.TestCase):
    """Test get_debounce_ms with relay-specific and generic fallback."""

    def test_relay_specific_var(self) -> None:
        with patch.dict(os.environ, {"FOO_LISTENER_DEBOUNCE_MS": "500"}, clear=True):
            self.assertEqual(get_debounce_ms(_FOO), 500)

    def test_falls_back_to_generic(self) -> None:
        with patch.dict(os.environ, {"LISTENER_DEBOUNCE_MS": "200"}, clear=True):
            self.assertEqual(get_debounce_ms(_FOO), 200)

    def test_relay_specific_takes_precedence(self) -> None:
        with patch.dict(os.environ, {
            "FOO_LISTENER_DEBOUNCE_MS": "100",
            "LISTENER_DEBOUNCE_MS": "200",
        }, clear=True):
            self.assertEqual(get_debounce_ms(_FOO), 100)

    def test_default_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_debounce_ms(_FOO), 0)

    def test_invalid_value_raises(self) -> None:
        with patch.dict(os.environ, {"FOO_LISTENER_DEBOUNCE_MS": "abc"}, clear=True), \
             self.assertRaises(SystemExit):
            get_debounce_ms(_FOO)

    def test_negative_value_raises(self) -> None:
        with patch.dict(os.environ, {"FOO_LISTENER_DEBOUNCE_MS": "-1"}, clear=True), \
             self.assertRaises(SystemExit):
            get_debounce_ms(_FOO)


class TestGetEnv(unittest.TestCase):
    """Test get_env — prefix fallback for string env vars."""

    def test_returns_prefixed_value(self) -> None:
        with patch.dict(os.environ, {"IBKR_MY_VAR": "prefixed"}, clear=True):
            self.assertEqual(get_env("MY_VAR", prefix="IBKR_"), "prefixed")

    def test_falls_back_to_generic(self) -> None:
        with patch.dict(os.environ, {"MY_VAR": "generic"}, clear=True):
            self.assertEqual(get_env("MY_VAR", prefix="IBKR_"), "generic")

    def test_prefixed_takes_precedence(self) -> None:
        env = {"IBKR_MY_VAR": "prefixed", "MY_VAR": "generic"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(get_env("MY_VAR", prefix="IBKR_"), "prefixed")

    def test_returns_default_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_env("MY_VAR", default="fallback"), "fallback")

    def test_suffix_appended(self) -> None:
        with patch.dict(os.environ, {"MY_VAR_2": "second"}, clear=True):
            self.assertEqual(get_env("MY_VAR", suffix="_2"), "second")

    def test_prefix_plus_suffix(self) -> None:
        with patch.dict(os.environ, {"IBKR_MY_VAR_2": "combo"}, clear=True):
            self.assertEqual(get_env("MY_VAR", prefix="IBKR_", suffix="_2"), "combo")

    def test_strips_whitespace(self) -> None:
        with patch.dict(os.environ, {"MY_VAR": "  spaced  "}, clear=True):
            self.assertEqual(get_env("MY_VAR"), "spaced")

    def test_blank_value_falls_back(self) -> None:
        env = {"IBKR_MY_VAR": "  ", "MY_VAR": "generic"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(get_env("MY_VAR", prefix="IBKR_"), "generic")

    def test_no_prefix_no_suffix(self) -> None:
        with patch.dict(os.environ, {"MY_VAR": "simple"}, clear=True):
            self.assertEqual(get_env("MY_VAR"), "simple")


class TestGetEnvInt(unittest.TestCase):
    """Test get_env_int — prefix fallback for integer env vars."""

    def test_returns_prefixed_value(self) -> None:
        with patch.dict(os.environ, {"IBKR_RETRIES": "3"}, clear=True):
            name, val = get_env_int("RETRIES", prefix="IBKR_")
            self.assertEqual(name, "IBKR_RETRIES")
            self.assertEqual(val, 3)

    def test_falls_back_to_generic(self) -> None:
        with patch.dict(os.environ, {"RETRIES": "2"}, clear=True):
            name, val = get_env_int("RETRIES", prefix="IBKR_")
            self.assertEqual(name, "RETRIES")
            self.assertEqual(val, 2)

    def test_returns_default_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            _name, val = get_env_int("RETRIES", default="5")
            self.assertEqual(val, 5)

    def test_invalid_value_raises(self) -> None:
        with patch.dict(os.environ, {"RETRIES": "abc"}, clear=True), \
             self.assertRaises(SystemExit):
            get_env_int("RETRIES")

    def test_suffix_appended(self) -> None:
        with patch.dict(os.environ, {"RETRIES_2": "4"}, clear=True):
            name, val = get_env_int("RETRIES", suffix="_2")
            self.assertEqual(name, "RETRIES_2")
            self.assertEqual(val, 4)

    def test_prefix_plus_suffix(self) -> None:
        with patch.dict(os.environ, {"IBKR_RETRIES_2": "1"}, clear=True):
            name, val = get_env_int("RETRIES", prefix="IBKR_", suffix="_2")
            self.assertEqual(name, "IBKR_RETRIES_2")
            self.assertEqual(val, 1)
