"""Tests for relay-agnostic env var getters."""

import os
import unittest
from typing import cast
from unittest.mock import patch

from relay_core import get_debounce_ms, get_poll_interval, is_listener_enabled, is_poller_enabled
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
