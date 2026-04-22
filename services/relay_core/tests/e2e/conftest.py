"""E2E test fixtures for the relayport test stack.

Smoke tests (health, auth) run unconditionally — they only need the relay stack.
Listener tests require an ibkr_bridge running locally and skip otherwise.

Stack must be up: make e2e-up
"""

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

BASE_URL = "http://localhost:15011"
API_TOKEN = "test-token"


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

BRIDGE_BASE_URL = os.environ.get("IBKR_BRIDGE_API_BASE_URL", "").strip()
BRIDGE_API_TOKEN = os.environ.get("IBKR_BRIDGE_API_TOKEN", "").strip()
LISTENER_ENABLED = os.environ.get("LISTENER_ENABLED", "").strip().lower() not in (
    "0", "false", "no", "",
)
DEBUG_INBOX_BASE = "http://localhost:15012"
DEBUG_INBOX_PATH = "/debug/webhook/test-debug-path"


# ---------------------------------------------------------------------------
# Stack preflight — fail fast if the relay stack is unreachable.
# autouse=True means pytest runs this fixture automatically for every test
# in this directory, without tests needing to request it explicitly.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _stack_preflight() -> None:
    """Abort the entire E2E run if the relay stack is not reachable."""
    try:
        resp = httpx.get(f"{BASE_URL}/health", timeout=5.0)
    except httpx.HTTPError:
        pytest.exit(
            f"Relay stack is not reachable at {BASE_URL}. "
            "Is the E2E stack running? (make e2e-up)",
            returncode=1,
        )
    if resp.status_code != 200:
        pytest.exit(
            f"Relay /health returned {resp.status_code}. "
            "Stack may not be ready yet.",
            returncode=1,
        )


# ---------------------------------------------------------------------------
# Smoke test fixtures (always available)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def api() -> Iterator[httpx.Client]:
    """Shared httpx client with auth header."""
    with httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {API_TOKEN}"},
        timeout=15.0,
    ) as client:
        yield client


@pytest.fixture(scope="session")
def anon_api() -> Iterator[httpx.Client]:
    """Httpx client without auth — for testing 401 responses."""
    with httpx.Client(base_url=BASE_URL, timeout=15.0) as client:
        yield client


# ---------------------------------------------------------------------------
# Listener/bridge fixtures (skip when bridge is unavailable)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def _bridge_preflight() -> None:
    """Skip listener tests if the bridge is not reachable."""
    if not LISTENER_ENABLED:
        pytest.skip(
            "LISTENER_ENABLED is not set — "
            "set LISTENER_ENABLED=true in .env.test to run listener E2E tests"
        )

    if not BRIDGE_API_TOKEN or not BRIDGE_BASE_URL:
        pytest.skip(
            "IBKR_BRIDGE_API_TOKEN / IBKR_BRIDGE_API_BASE_URL not set — "
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
def bridge_api(_bridge_preflight: None) -> Iterator[httpx.Client]:
    """httpx client pointed at the local ibkr_bridge API."""
    with httpx.Client(
        base_url=BRIDGE_BASE_URL,
        headers={"Authorization": f"Bearer {BRIDGE_API_TOKEN}"},
        timeout=30.0,
    ) as client:
        yield client


@pytest.fixture(scope="session")
def debug_api(_bridge_preflight: None) -> Iterator[httpx.Client]:
    """httpx client pointed at the debug webhook inbox."""
    with httpx.Client(
        base_url=DEBUG_INBOX_BASE,
        timeout=10.0,
    ) as client:
        yield client
