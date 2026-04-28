"""Tests for KrakenClient.get_ws_token validation."""

import threading
import time
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from .rest_client import KrakenClient

# base64.b64encode(b"test-secret") -> "dGVzdC1zZWNyZXQ="
_KEY = "test-api-key"
_SECRET = "dGVzdC1zZWNyZXQ="


def _make_client() -> KrakenClient:
    return KrakenClient(api_key=_KEY, api_secret=_SECRET)


class TestGetWsTokenValidation(unittest.TestCase):
    """get_ws_token must reject responses that lack a valid token."""

    def _call_with_result(self, result: object) -> str:
        client = _make_client()
        with patch.object(client, "_request", return_value=result):
            return client.get_ws_token()

    def test_valid_token_returned(self) -> None:
        token = self._call_with_result({"token": "abc123"})
        self.assertEqual(token, "abc123")

    def test_missing_token_key_raises(self) -> None:
        with self.assertRaises(RuntimeError) as cm:
            self._call_with_result({"expires": 900})
        self.assertIn("unexpected payload", str(cm.exception))

    def test_result_not_a_dict_raises(self) -> None:
        with self.assertRaises(RuntimeError) as cm:
            self._call_with_result(["token", "abc123"])
        self.assertIn("unexpected payload", str(cm.exception))

    def test_result_none_raises(self) -> None:
        with self.assertRaises(RuntimeError) as cm:
            self._call_with_result(None)
        self.assertIn("unexpected payload", str(cm.exception))

    def test_token_empty_string_raises(self) -> None:
        with self.assertRaises(RuntimeError) as cm:
            self._call_with_result({"token": ""})
        self.assertIn("invalid token value", str(cm.exception))

    def test_token_not_a_string_raises(self) -> None:
        with self.assertRaises(RuntimeError) as cm:
            self._call_with_result({"token": 12345})
        self.assertIn("invalid token value", str(cm.exception))


class TestNonceMonotonic(unittest.TestCase):
    """_next_nonce must produce a strictly increasing sequence even under contention."""

    def test_consecutive_calls_strictly_increase(self) -> None:
        client = _make_client()
        previous = client._next_nonce()
        for _ in range(1000):
            current = client._next_nonce()
            self.assertGreater(current, previous)
            previous = current

    def test_concurrent_threads_produce_unique_nonces_no_duplicates(self) -> None:
        client = _make_client()
        results: list[int] = []
        results_lock = threading.Lock()

        def worker() -> None:
            local: list[int] = []
            for _ in range(200):
                # Mirror production call ordering: _request acquires the
                # same lock around _next_nonce, so two threads cannot
                # observe an interleaved nonce.
                with client._request_lock:
                    local.append(client._next_nonce())
            with results_lock:
                results.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 8 * 200)
        self.assertEqual(len(set(results)), len(results))


class TestRequestSerialization(unittest.TestCase):
    """_request must not let a second thread enter httpx.post until the first completes."""

    def test_second_thread_blocked_and_nonces_strictly_ordered(self) -> None:
        client = _make_client()

        first_entered = threading.Event()
        release_first = threading.Event()
        nonces_sent: list[int] = []
        active_lock = threading.Lock()
        active_in_post = 0
        max_concurrent_in_post = 0

        def mock_post(*args: Any, **kwargs: Any) -> MagicMock:
            nonlocal active_in_post, max_concurrent_in_post
            with active_lock:
                active_in_post += 1
                if active_in_post > max_concurrent_in_post:
                    max_concurrent_in_post = active_in_post
            data: dict[str, Any] = kwargs.get("data", {})
            nonces_sent.append(int(data["nonce"]))
            if not first_entered.is_set():
                first_entered.set()
                release_first.wait()
            with active_lock:
                active_in_post -= 1
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = {"error": [], "result": {}}
            return resp

        with patch("relays.kraken.rest_client.httpx.post", side_effect=mock_post):
            t2_done = threading.Event()

            def thread2() -> None:
                first_entered.wait()
                client._request("/0/private/GetWebSocketsToken")
                t2_done.set()

            t1 = threading.Thread(target=client._request, args=("/0/private/TradesHistory",))
            t2 = threading.Thread(target=thread2)

            t1.start()
            first_entered.wait()
            t2.start()
            time.sleep(0.05)  # let t2 reach and block on _request_lock

            self.assertEqual(
                len(nonces_sent),
                1,
                "t2 must not enter httpx.post while t1 holds _request_lock",
            )
            self.assertEqual(max_concurrent_in_post, 1)

            release_first.set()
            t1.join(timeout=2)
            self.assertTrue(t2_done.wait(timeout=2))

            self.assertEqual(len(nonces_sent), 2)
            self.assertLess(
                nonces_sent[0],
                nonces_sent[1],
                "nonces must be strictly increasing in the order httpx.post receives them",
            )
            self.assertEqual(max_concurrent_in_post, 1, "serialization must hold throughout")
