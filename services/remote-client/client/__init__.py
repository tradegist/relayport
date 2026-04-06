"""IB Gateway client — connection management and namespace delegation."""

import asyncio
import logging
import os

from ib_async import IB

from client.listener import ListenerNamespace
from client.orders import OrdersNamespace
from client.trades import TradesNamespace

log = logging.getLogger("ib-client")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
IB_HOST = os.environ.get("IB_HOST", "ib-gateway")
TRADING_MODE = os.environ.get("TRADING_MODE", "paper")
IB_PORT = int(os.environ.get(
    "IB_LIVE_PORT" if TRADING_MODE == "live" else "IB_PAPER_PORT", "4004"
))
CLIENT_ID = 1

INITIAL_RETRY_DELAY = 10
MAX_RETRY_DELAY = 300


class IBClient:
    """Thin wrapper around ib_async.IB for connection management."""

    def __init__(self) -> None:
        self.ib = IB()
        self._retry_delay = INITIAL_RETRY_DELAY
        self._background_tasks: set[asyncio.Task[None]] = set()
        self.orders = OrdersNamespace(self.ib)
        self.trades = TradesNamespace(self.ib)
        self.listener: ListenerNamespace | None = None

    @property
    def is_connected(self) -> bool:
        return self.ib.isConnected()

    async def connect(self) -> None:
        """Connect to IB Gateway with exponential backoff retry."""
        while True:
            try:
                log.info("Connecting to IB Gateway at %s:%d ...", IB_HOST, IB_PORT)
                await self.ib.connectAsync(
                    IB_HOST, IB_PORT, clientId=CLIENT_ID, timeout=20
                )
                log.info("Connected — %d account(s)", len(self.ib.managedAccounts()))
                self._retry_delay = INITIAL_RETRY_DELAY
                return
            except Exception as exc:
                log.warning(
                    "Connection failed: %s — retrying in %ds",
                    exc, self._retry_delay,
                )
                await asyncio.sleep(self._retry_delay)
                self._retry_delay = min(self._retry_delay * 2, MAX_RETRY_DELAY)

    def on_disconnect(self) -> None:
        log.warning("Disconnected from IB Gateway — will reconnect")
        task = asyncio.ensure_future(self._reconnect())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _reconnect(self) -> None:
        await asyncio.sleep(self._retry_delay)
        if not self.is_connected:
            await self.connect()

    async def watchdog(self) -> None:
        """Periodically check the connection and reconnect if stale."""
        while True:
            await asyncio.sleep(30)
            if not self.is_connected:
                log.warning("Watchdog: connection lost — reconnecting")
                await self.connect()
