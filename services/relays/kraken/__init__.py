"""Kraken relay adapter — crypto exchange integration.

Wires Kraken REST polling and WebSocket v2 listening into the
generic ``relay_core`` engines.  All Kraken-specific logic lives here:
env var getters, REST fetch/parse, WS connect/subscribe/parse.
"""

import json
import logging
import os
import time
from typing import Any, cast

import aiohttp

from relay_core import (
    BaseNotifier,
    BrokerRelay,
    ListenerConfig,
    OnMessageResult,
    PollerConfig,
    get_debounce_ms,
    get_poll_interval,
    is_listener_enabled,
    is_poller_enabled,
)
from shared import BuySell, Fill

from .kraken_types import KrakenRestTrade, KrakenWsMessage
from .rest_client import KrakenClient
from .ws_parser import normalize_order_type, parse_executions

log = logging.getLogger("relays.kraken")

_WS_URL = "wss://ws-auth.kraken.com/v2"


# ── Env var getters (Kraken-specific) ─────────────────────────────


def _get_api_key() -> str | None:
    return os.environ.get("KRAKEN_API_KEY", "").strip() or None


def _get_api_secret() -> str | None:
    return os.environ.get("KRAKEN_API_SECRET", "").strip() or None


# ── REST poller adapter ──────────────────────────────────────────


def _parse_rest_trade(txid: str, data: KrakenRestTrade) -> Fill:
    """Convert a single REST API trade entry to a Fill model."""
    side_str = data.get("type", "")
    if side_str == "buy":
        side = BuySell.BUY
    elif side_str == "sell":
        side = BuySell.SELL
    else:
        raise ValueError(f"Invalid trade side: {side_str!r}")

    trade_time = float(data.get("time", 0))
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(trade_time))

    order_type = normalize_order_type(str(data.get("ordertype", "")))

    return Fill(
        execId=txid,
        orderId=str(data.get("ordertxid", "")),
        symbol=str(data.get("pair", "")),
        assetClass="crypto",
        side=side,
        orderType=order_type,
        price=float(data.get("price", 0)),
        volume=float(data.get("vol", 0)),
        cost=float(data.get("cost", 0)),
        fee=abs(float(data.get("fee", 0))),
        timestamp=ts,
        source="rest_poll",
        raw={"txid": txid, **data},
    )


def _build_fetch(client: KrakenClient) -> Any:
    """Return a fetch callable for the generic poller engine.

    Returns a JSON string of the raw trades dict, or None on failure.
    """

    def fetch() -> str | None:
        try:
            result = client.get_trades_history()
            return json.dumps(result)
        except Exception:
            log.exception("Failed to fetch trades from Kraken")
            return None

    return fetch


def _build_parse() -> Any:
    """Return a parse callable for the generic poller engine."""

    def parse(raw: str) -> tuple[list[Fill], list[str]]:
        try:
            result: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            return [], [f"Failed to parse Kraken REST response JSON: {exc}"]
        raw_trades: dict[str, KrakenRestTrade] = result.get("trades", {})

        fills: list[Fill] = []
        errors: list[str] = []

        for txid, trade_data in raw_trades.items():
            try:
                fill = _parse_rest_trade(txid, trade_data)
                fills.append(fill)
            except Exception as exc:
                errors.append(f"Failed to parse trade {txid}: {exc}")

        return fills, errors

    return parse


def _build_poller_configs() -> list[PollerConfig]:
    """Build PollerConfig(s) from env vars.

    Returns an empty list when polling is disabled or no API
    credentials are configured (listener-only mode).
    """
    if not is_poller_enabled("kraken"):
        return []

    api_key = _get_api_key()
    api_secret = _get_api_secret()

    if not api_key and not api_secret:
        return []

    if not api_key or not api_secret:
        missing = "KRAKEN_API_SECRET" if api_key else "KRAKEN_API_KEY"
        raise SystemExit(
            f"Kraken poller partially configured — {missing} must be set"
        )

    try:
        client = KrakenClient(api_key, api_secret)
    except RuntimeError as exc:
        raise SystemExit(f"Kraken poller config error: {exc}") from exc
    interval = get_poll_interval("kraken")

    return [PollerConfig(
        fetch=_build_fetch(client),
        parse=_build_parse(),
        interval=interval,
    )]


# ── WebSocket v2 listener adapter ────────────────────────────────


def _event_filter(data: dict[str, Any]) -> bool:
    """Return True for events the Kraken adapter handles."""
    # Skip heartbeats
    if data.get("channel") == "heartbeat":
        return False

    # Skip subscription acks
    if data.get("method") in ("subscribe", "unsubscribe"):
        success = data.get("success", False)
        log.info("Kraken subscription response: success=%s", success)
        return False

    # Only process executions channel
    return data.get("channel") == "executions"


async def _on_message(data: dict[str, Any]) -> list[OnMessageResult]:
    """Parse a Kraken WS v2 executions message into fills."""
    msg = cast(KrakenWsMessage, data)
    fills, errors = parse_executions(msg)

    if errors:
        for err in errors:
            log.warning("Kraken WS parse error: %s", err)

    return [OnMessageResult(fill=fill, mark=True) for fill in fills]


def _build_connect(client: KrakenClient) -> Any:
    """Build a connect callback that obtains a WS token, connects, and subscribes."""

    async def connect(
        session: aiohttp.ClientSession,
    ) -> aiohttp.ClientWebSocketResponse:
        # Obtain short-lived WS token via REST API (blocking, run in thread)
        import asyncio
        token = await asyncio.to_thread(client.get_ws_token)

        ws = await session.ws_connect(_WS_URL)

        # Subscribe to executions channel
        sub_msg = {
            "method": "subscribe",
            "params": {
                "channel": "executions",
                "snap_trades": False,
                "snap_orders": False,
                "token": token,
            },
        }
        await ws.send_json(sub_msg)
        log.info("Subscribed to Kraken executions channel")
        return ws

    return connect


def _build_listener_config() -> ListenerConfig | None:
    """Build ListenerConfig if listener is enabled, else return None."""
    if not is_listener_enabled("kraken"):
        return None

    api_key = _get_api_key()
    api_secret = _get_api_secret()

    if not api_key or not api_secret:
        raise SystemExit(
            "Kraken listener enabled but KRAKEN_API_KEY and "
            "KRAKEN_API_SECRET must be set"
        )

    try:
        client = KrakenClient(api_key, api_secret)
    except RuntimeError as exc:
        raise SystemExit(f"Kraken listener config error: {exc}") from exc

    return ListenerConfig(
        connect=_build_connect(client),
        on_message=_on_message,
        event_filter=_event_filter,
        debounce_ms=get_debounce_ms("kraken"),
    )


# ── Public API ───────────────────────────────────────────────────


def build_relay(notifiers: list[BaseNotifier]) -> BrokerRelay:
    """Build a fully configured Kraken relay instance."""
    poller_configs = _build_poller_configs()
    listener_config = _build_listener_config()

    if not poller_configs and listener_config is None:
        raise SystemExit(
            "Kraken relay has neither poller nor listener configured. "
            "Set KRAKEN_API_KEY + KRAKEN_API_SECRET for polling, "
            "or KRAKEN_LISTENER_ENABLED=true for real-time events."
        )

    if not poller_configs:
        log.info("Kraken: listener-only mode (poller disabled or no credentials)")
    if listener_config is None:
        log.info("Kraken: poller-only mode (listener disabled)")

    return BrokerRelay(
        name="kraken",
        notifiers=notifiers,
        poller_configs=poller_configs,
        listener_config=listener_config,
    )
