"""E2E test fixtures — listener tests against a local ibkr_bridge stack.

These tests require:
1. ibkr_bridge running locally (make local-up in ibkr_bridge)
2. BRIDGE_API_TOKEN set in .env.test (must match bridge's API_TOKEN)
3. ibkr_relay E2E stack running (make e2e-up)

Tests SKIP (not fail) when the bridge is unavailable or unconfigured.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest


def _load_env_test() -> None:
    """Load .env.test into os.environ (simple key=value, no overwrite)."""
    env_file = Path(__file__).resolve().parents[4] / ".env.test"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env_test()

BRIDGE_BASE_URL = os.environ.get("BRIDGE_API_BASE_URL", "").strip()
BRIDGE_API_TOKEN = os.environ.get("BRIDGE_API_TOKEN", "").strip()
LISTENER_ENABLED = os.environ.get("LISTENER_ENABLED", "").strip().lower() not in (
    "0", "false", "no", "",
)
DEBUG_INBOX_BASE = "http://localhost:15012"
DEBUG_INBOX_PATH = "/debug/webhook/test-debug-path"


@pytest.fixture(scope="session", autouse=True)
def _bridge_preflight() -> None:
    """Skip all listener E2E tests if the bridge is not reachable."""
    if not LISTENER_ENABLED:
        pytest.skip(
            "LISTENER_ENABLED is not set — "
            "set LISTENER_ENABLED=true in .env.test to run listener E2E tests"
        )

    if not BRIDGE_API_TOKEN or not BRIDGE_BASE_URL:
        pytest.skip(
            "BRIDGE_API_TOKEN / BRIDGE_API_BASE_URL not set — "
            "skipping listener E2E tests"
        )

    try:
        resp = httpx.get(f"{BRIDGE_BASE_URL}/health", timeout=5.0)
    except httpx.HTTPError:
        pytest.skip(
            f"Bridge not reachable at {BRIDGE_BASE_URL} — "
            "is ibkr_bridge local stack running? (make local-up)"
        )

    if resp.status_code != 200:
        pytest.skip(f"Bridge /health returned {resp.status_code}")

    body = resp.json()
    if not body.get("connected"):
        pytest.skip("Bridge is not connected to IB Gateway")


@pytest.fixture(scope="session")
def bridge_api() -> Iterator[httpx.Client]:
    """httpx client pointed at the local ibkr_bridge API."""
    with httpx.Client(
        base_url=BRIDGE_BASE_URL,
        headers={"Authorization": f"Bearer {BRIDGE_API_TOKEN}"},
        timeout=30.0,
    ) as client:
        yield client


@pytest.fixture(scope="session")
def debug_api() -> Iterator[httpx.Client]:
    """httpx client pointed at the ibkr-debug webhook inbox."""
    with httpx.Client(
        base_url=DEBUG_INBOX_BASE,
        timeout=10.0,
    ) as client:
        yield client
