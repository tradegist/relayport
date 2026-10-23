"""Unit tests for relay_core.main — poll loop outage reporting."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

from relay_core import BrokerRelay, FetchConfigError, FetchError, PollerConfig
from relay_core.health import OutageAlert
from relay_core.main import NEEDS_ACTION_PAUSE_S, _poll_loop
from relay_core.notifier import NotificationError


def _relay(pollers: int = 1, interval: int = 600) -> BrokerRelay:
    configs = [
        PollerConfig(fetch=lambda: "<xml/>", parse=lambda _raw: ([], []), interval=interval)
        for _ in range(pollers)
    ]
    relay = BrokerRelay(
        name="ibkr", notifiers=[], poller_configs=configs, outage_alert_after_s=900,
    )
    relay.poll_locks = [asyncio.Lock() for _ in configs]
    return relay


_ALERT = OutageAlert(subject="s", body="b", key="outage:ibkr:poller")


@patch("relay_core.main.send_alert")
@patch("relay_core.main.OutageTracker")
@patch("relay_core.main.poll_once")
class TestPollLoopOutageReporting(unittest.IsolatedAsyncioTestCase):
    """Each cycle's outcome reaches the tracker; its alerts reach send_alert.

    poll_once's last side effect is CancelledError, which ends the loop.
    asyncio.sleep is mocked, so self.mock_sleep records each delay chosen.
    """

    def setUp(self) -> None:
        patcher = patch("relay_core.main.asyncio.sleep", new_callable=AsyncMock)
        self.mock_sleep = patcher.start()
        self.addCleanup(patcher.stop)

    async def _run(
        self, mock_poll: MagicMock, *outcomes: object, relay: BrokerRelay | None = None,
    ) -> None:
        mock_poll.side_effect = [*outcomes, asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await _poll_loop(relay or _relay(), 0)

    async def test_fetch_error_records_failure_with_reason(
        self, mock_poll: MagicMock, mock_tracker_cls: MagicMock, mock_send: MagicMock,
    ) -> None:
        tracker = mock_tracker_cls.return_value
        tracker.record_failure.return_value = None
        await self._run(mock_poll, FetchError("Flex SendRequest failed: [1018] Too many requests"))
        mock_tracker_cls.assert_called_once_with("ibkr", "poller", 900)
        tracker.record_failure.assert_called_once_with(
            "Flex SendRequest failed: [1018] Too many requests",
            needs_action=False, retry_in_s=None,
        )
        mock_send.assert_not_called()
        # A transient failure keeps the normal interval.
        self.assertEqual(self.mock_sleep.call_args_list, [call(600)])

    async def test_fetch_config_error_needs_action(
        self, mock_poll: MagicMock, mock_tracker_cls: MagicMock, mock_send: MagicMock,
    ) -> None:
        tracker = mock_tracker_cls.return_value
        tracker.record_failure.return_value = _ALERT
        await self._run(
            mock_poll, FetchConfigError("Flex SendRequest failed: [1015] Token is invalid."),
        )
        tracker.record_failure.assert_called_once_with(
            "Flex SendRequest failed: [1015] Token is invalid.",
            needs_action=True, retry_in_s=NEEDS_ACTION_PAUSE_S,
        )
        mock_send.assert_called_once_with(subject="s", body="b", key="outage:ibkr:poller")
        self.assertEqual(self.mock_sleep.call_args_list, [call(NEEDS_ACTION_PAUSE_S)])

    async def test_needs_action_pause_is_two_hours(
        self, mock_poll: MagicMock, mock_tracker_cls: MagicMock, mock_send: MagicMock,
    ) -> None:
        self.assertEqual(NEEDS_ACTION_PAUSE_S, 2 * 3600)

    async def test_pause_never_shortens_a_longer_interval(
        self, mock_poll: MagicMock, mock_tracker_cls: MagicMock, mock_send: MagicMock,
    ) -> None:
        mock_tracker_cls.return_value.record_failure.return_value = None
        await self._run(
            mock_poll, FetchConfigError("[1012] Token has expired."),
            relay=_relay(interval=4 * 3600),
        )
        self.assertEqual(self.mock_sleep.call_args_list, [call(4 * 3600)])

    async def test_poll_after_pause_returns_to_normal_interval(
        self, mock_poll: MagicMock, mock_tracker_cls: MagicMock, mock_send: MagicMock,
    ) -> None:
        tracker = mock_tracker_cls.return_value
        tracker.record_failure.return_value = None
        tracker.record_success.return_value = None
        await self._run(mock_poll, FetchConfigError("[1012] Token has expired."), [])
        self.assertEqual(
            self.mock_sleep.call_args_list, [call(NEEDS_ACTION_PAUSE_S), call(600)],
        )

    async def test_success_records_success(
        self, mock_poll: MagicMock, mock_tracker_cls: MagicMock, mock_send: MagicMock,
    ) -> None:
        tracker = mock_tracker_cls.return_value
        tracker.record_success.return_value = None
        await self._run(mock_poll, [])
        tracker.record_success.assert_called_once_with()
        tracker.record_failure.assert_not_called()

    async def test_notification_error_counts_as_fetch_success(
        self, mock_poll: MagicMock, mock_tracker_cls: MagicMock, mock_send: MagicMock,
    ) -> None:
        """Delivery failures alert from the notifier — not a broker outage."""
        tracker = mock_tracker_cls.return_value
        tracker.record_success.return_value = None
        await self._run(mock_poll, NotificationError([("WebhookNotifier", RuntimeError("503"))]))
        tracker.record_success.assert_called_once_with()
        tracker.record_failure.assert_not_called()

    async def test_unexpected_error_reports_type_only(
        self, mock_poll: MagicMock, mock_tracker_cls: MagicMock, mock_send: MagicMock,
    ) -> None:
        tracker = mock_tracker_cls.return_value
        tracker.record_failure.return_value = None
        await self._run(mock_poll, KeyError("https://example.com/?t=secret"))
        reason = tracker.record_failure.call_args.args[0]
        self.assertEqual(reason, "Poll cycle crashed with KeyError — see relay logs")
        self.assertNotIn("secret", reason)

    async def test_tracker_alert_is_sent(
        self, mock_poll: MagicMock, mock_tracker_cls: MagicMock, mock_send: MagicMock,
    ) -> None:
        tracker = mock_tracker_cls.return_value
        tracker.record_failure.return_value = _ALERT
        await self._run(mock_poll, FetchError("down"))
        mock_send.assert_called_once_with(subject="s", body="b", key="outage:ibkr:poller")

    async def test_multi_poller_relay_names_each_poller(
        self, mock_poll: MagicMock, mock_tracker_cls: MagicMock, mock_send: MagicMock,
    ) -> None:
        mock_poll.side_effect = [asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await _poll_loop(_relay(pollers=2), 1)
        self.assertEqual(mock_tracker_cls.call_args, call("ibkr", "poller 2", 900))
