"""Shared fixtures for relay_core tests."""

from collections.abc import Generator

import pytest

from relay_core import BrokerRelay
from relay_core.context import _reset, init_relays


@pytest.fixture(autouse=True)
def _init_relay_context() -> Generator[None]:
    """Initialise the relay context with a default test relay.

    Uses autouse so every relay_core test has access to ``get_relay("ibkr")``.
    Resets after each test to avoid cross-test leakage.

    Tests that need custom relay config (e.g. with specific PollerConfigs)
    should call ``_reset()`` + ``init_relays(...)`` within the test body.
    """
    _reset()
    init_relays([BrokerRelay(name="ibkr", notifiers=[])])
    yield
    _reset()
