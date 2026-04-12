"""Unit tests for relay_core.routes (health, poll, auth middleware)."""

import asyncio
import json
import os
import unittest
from typing import cast
from unittest.mock import MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from relay_core import BrokerRelay, PollerConfig
from relay_core.routes import _RELAYS_KEY, create_app
from shared import RelayName

_TEST_TOKEN = "test-secret-token"


def _make_relay(
    name: str = "ibkr",
    *,
    with_poller: bool = True,
) -> BrokerRelay:
    """Build a minimal BrokerRelay for testing."""
    configs: list[PollerConfig] = []
    if with_poller:
        configs.append(
            PollerConfig(
                fetch=lambda: "<xml/>",
                parse=lambda _raw: ([], []),
                interval=600,
            )
        )
    relay = BrokerRelay(
        name=cast(RelayName, name),
        notifiers=[],
        poller_configs=configs,
    )
    relay.poll_locks = [asyncio.Lock() for _ in configs]
    return relay


# ── Auth middleware tests ────────────────────────────────────────────


class TestAuthMiddleware(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        return create_app([_make_relay()])

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    async def test_health_bypasses_auth(self) -> None:
        resp = await self.client.request("GET", "/health")
        self.assertEqual(resp.status, 200)

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    async def test_missing_auth_header(self) -> None:
        resp = await self.client.request("POST", "/relays/ibkr/poll/1")
        self.assertEqual(resp.status, 401)

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    async def test_invalid_token(self) -> None:
        resp = await self.client.request(
            "POST", "/relays/ibkr/poll/1",
            headers={"Authorization": "Bearer wrong"},
        )
        self.assertEqual(resp.status, 401)

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    async def test_valid_token(self) -> None:
        resp = await self.client.request(
            "POST", "/relays/ibkr/poll/1",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
        )
        # Should reach the handler (200 or other, not 401)
        self.assertNotEqual(resp.status, 401)

    @patch.dict(os.environ, {"API_TOKEN": ""})
    async def test_empty_api_token_rejects(self) -> None:
        resp = await self.client.request(
            "POST", "/relays/ibkr/poll/1",
            headers={"Authorization": "Bearer "},
        )
        self.assertEqual(resp.status, 500)
        body = await resp.json()
        self.assertIn("misconfigured", body["error"].lower())

    @patch.dict(os.environ, {"API_TOKEN": ""})
    async def test_empty_api_token_health_still_works(self) -> None:
        resp = await self.client.request("GET", "/health")
        self.assertEqual(resp.status, 200)


# ── Health handler tests ─────────────────────────────────────────────


class TestHealthHandler(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        return create_app([_make_relay()])

    async def test_health_returns_ok(self) -> None:
        resp = await self.client.request("GET", "/health")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["status"], "ok")


# ── Poll handler tests ───────────────────────────────────────────────


class TestPollHandler(AioHTTPTestCase):
    async def get_application(self) -> web.Application:
        return create_app([_make_relay()])

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    @patch("relay_core.routes.poll_once")
    async def test_poll_success_empty(self, mock_poll: MagicMock) -> None:
        mock_poll.return_value = []
        resp = await self.client.request(
            "POST", "/relays/ibkr/poll/1",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
        )
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["trades"], [])
        mock_poll.assert_called_once()

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    async def test_poll_unknown_relay(self) -> None:
        resp = await self.client.request(
            "POST", "/relays/fake/poll/1",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
        )
        self.assertEqual(resp.status, 404)
        body = await resp.json()
        self.assertIn("Unknown relay", body["error"])

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    @patch("relay_core.routes.poll_once")
    async def test_poll_passes_replay(self, mock_poll: MagicMock) -> None:
        mock_poll.return_value = []
        resp = await self.client.request(
            "POST", "/relays/ibkr/poll/1",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
            data=json.dumps({"replay": 5}),
        )
        self.assertEqual(resp.status, 200)
        # Verify replay was forwarded
        call_kwargs = mock_poll.call_args
        self.assertEqual(call_kwargs.kwargs.get("replay"), 5)

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    @patch("relay_core.routes.poll_once", side_effect=RuntimeError("boom"))
    async def test_poll_exception_returns_500(self, _mock: MagicMock) -> None:
        resp = await self.client.request(
            "POST", "/relays/ibkr/poll/1",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
        )
        self.assertEqual(resp.status, 500)
        body = await resp.json()
        self.assertEqual(body["error"], "Internal server error")

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    async def test_poll_idx_out_of_bounds(self) -> None:
        resp = await self.client.request(
            "POST", "/relays/ibkr/poll/99",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
        )
        self.assertEqual(resp.status, 404)
        body = await resp.json()
        self.assertIn("not configured", body["error"].lower())

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    async def test_poll_idx_zero_returns_400(self) -> None:
        resp = await self.client.request(
            "POST", "/relays/ibkr/poll/0",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
        )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertIn("must be a positive integer", body["error"])

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    async def test_poll_idx_negative_returns_400(self) -> None:
        resp = await self.client.request(
            "POST", "/relays/ibkr/poll/-1",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
        )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertIn("must be a positive integer", body["error"])


class TestPollReplayValidation(AioHTTPTestCase):
    """Replay body parameter validation (CR regression tests)."""

    async def get_application(self) -> web.Application:
        return create_app([_make_relay()])

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    async def test_replay_non_numeric_returns_400(self) -> None:
        """Invalid replay like 'abc' must not silently become 0."""
        resp = await self.client.request(
            "POST", "/relays/ibkr/poll/1",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
            data=json.dumps({"replay": "abc"}),
        )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertIn("replay", body["error"].lower())

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    async def test_replay_negative_returns_400(self) -> None:
        """Negative replay must be rejected."""
        resp = await self.client.request(
            "POST", "/relays/ibkr/poll/1",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
            data=json.dumps({"replay": -3}),
        )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertIn(">= 0", body["error"])

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    async def test_malformed_json_body_returns_400(self) -> None:
        """Non-JSON body must return 400, not be silently ignored."""
        resp = await self.client.request(
            "POST", "/relays/ibkr/poll/1",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
            data="not json",
        )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertIn("valid JSON", body["error"])

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    async def test_json_array_body_returns_400(self) -> None:
        """JSON body that is not an object must return 400."""
        resp = await self.client.request(
            "POST", "/relays/ibkr/poll/1",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
            data=json.dumps([1, 2, 3]),
        )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertIn("JSON object", body["error"])

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    @patch("relay_core.routes.poll_once")
    async def test_no_body_defaults_replay_zero(self, mock_poll: MagicMock) -> None:
        """POST with no body should still work with replay=0."""
        mock_poll.return_value = []
        resp = await self.client.request(
            "POST", "/relays/ibkr/poll/1",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(mock_poll.call_args.kwargs.get("replay"), 0)


class TestPollNoPollers(AioHTTPTestCase):
    """Relay with no poller_configs returns 400."""

    async def get_application(self) -> web.Application:
        return create_app([_make_relay(with_poller=False)])

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    async def test_poll_no_pollers(self) -> None:
        resp = await self.client.request(
            "POST", "/relays/ibkr/poll/1",
            headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
        )
        self.assertEqual(resp.status, 400)
        body = await resp.json()
        self.assertIn("no pollers", body["error"].lower())


class TestPollLockConflict(AioHTTPTestCase):
    """Concurrent poll requests return 409."""

    async def get_application(self) -> web.Application:
        return create_app([_make_relay()])

    @patch.dict(os.environ, {"API_TOKEN": _TEST_TOKEN})
    @patch("relay_core.routes.poll_once")
    async def test_concurrent_poll_returns_409(self, _mock_poll: MagicMock) -> None:
        # Lock the poll_lock manually to simulate a poll already in progress.
        relays: dict[str, BrokerRelay] = self.app[_RELAYS_KEY]
        relay = relays["ibkr"]
        await relay.poll_locks[0].acquire()

        try:
            resp = await self.client.request(
                "POST", "/relays/ibkr/poll/1",
                headers={"Authorization": f"Bearer {_TEST_TOKEN}"},
            )
            self.assertEqual(resp.status, 409)
            body = await resp.json()
            self.assertIn("already in progress", body["error"].lower())
        finally:
            relay.poll_locks[0].release()


# ── get_api_port tests ───────────────────────────────────────────────


class TestGetApiPort(unittest.TestCase):
    @patch.dict(os.environ, {"API_PORT": "9090"}, clear=False)
    def test_reads_api_port(self) -> None:
        from relay_core.routes import get_api_port
        self.assertEqual(get_api_port(), 9090)

    @patch.dict(os.environ, {"API_PORT": ""}, clear=False)
    def test_default_8000(self) -> None:
        from relay_core.routes import get_api_port
        self.assertEqual(get_api_port(), 8000)

    @patch.dict(os.environ, {"API_PORT": "abc"}, clear=False)
    def test_invalid_raises_system_exit(self) -> None:
        from relay_core.routes import get_api_port
        with self.assertRaises(SystemExit):
            get_api_port()


if __name__ == "__main__":
    unittest.main()
