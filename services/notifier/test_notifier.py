"""Unit tests for notifier registry, loader, and dispatcher."""

from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from notifier import REGISTRY, load_notifiers, notify


class _SamplePayload(BaseModel):
    symbol: str


class TestRegistry:
    def test_webhook_registered(self) -> None:
        assert "webhook" in REGISTRY

    def test_registry_values_are_classes(self) -> None:
        from notifier.base import BaseNotifier

        for cls in REGISTRY.values():
            assert issubclass(cls, BaseNotifier)


class TestLoadNotifiers:
    def test_empty_env_returns_empty(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = load_notifiers()
        assert result == []

    def test_blank_env_returns_empty(self) -> None:
        with patch.dict("os.environ", {"NOTIFIERS": "  "}, clear=True):
            result = load_notifiers()
        assert result == []

    def test_unknown_name_exits(self) -> None:
        with patch.dict("os.environ", {"NOTIFIERS": "bogus"}, clear=True), \
             pytest.raises(SystemExit):
            load_notifiers()

    def test_missing_required_vars_exits(self) -> None:
        with patch.dict("os.environ", {"NOTIFIERS": "webhook"}, clear=True), \
             pytest.raises(SystemExit):
            load_notifiers()

    def test_valid_config_returns_instances(self) -> None:
        env = {
            "NOTIFIERS": "webhook",
            "TARGET_WEBHOOK_URL": "https://example.com/hook",
            "WEBHOOK_SECRET": "s",
        }
        with patch.dict("os.environ", env, clear=True):
            result = load_notifiers()
        assert len(result) == 1
        assert result[0].name == "webhook"

    def test_suffix_reads_suffixed_vars(self) -> None:
        env = {
            "NOTIFIERS_2": "webhook",
            "TARGET_WEBHOOK_URL_2": "https://example.com/hook2",
            "WEBHOOK_SECRET_2": "secret2",
        }
        with patch.dict("os.environ", env, clear=True):
            result = load_notifiers(suffix="_2")
        assert len(result) == 1


class TestNotify:
    def test_dispatches_to_all(self) -> None:
        n1 = MagicMock()
        n2 = MagicMock()
        payload = _SamplePayload(symbol="AAPL")

        notify([n1, n2], payload)

        n1.send.assert_called_once_with(payload)
        n2.send.assert_called_once_with(payload)

    def test_empty_list_is_noop(self) -> None:
        notify([], _SamplePayload(symbol="AAPL"))  # should not raise
