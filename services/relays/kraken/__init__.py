"""Kraken relay adapter — crypto exchange integration.

Wires Kraken REST polling and WebSocket v2 listening into the
generic ``relay_core`` engines.  All Kraken-specific logic lives here:
env var getters, REST fetch/parse, WS connect/subscribe/parse.
"""

import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

import aiohttp

from relay_core import (
    BaseNotifier,
    BrokerRelay,
    FatalListenerError,
    ListenerConfig,
    OnMessageResult,
    PollerConfig,
    get_debounce_ms,
    get_poll_interval,
    is_listener_enabled,
    is_poller_enabled,
)
from relay_core.parsing import require_float, require_str
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
    if not txid:
        raise ValueError("Missing required REST trade field 'txid'")

    ctx = f"REST trade {txid}"

    side_str = require_str(data, "type", ctx)
    if side_str == "buy":
        side = BuySell.BUY
    elif side_str == "sell":
        side = BuySell.SELL
    else:
        raise ValueError(f"{ctx}: invalid trade side {side_str!r}")

    ts = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(require_float(data, "time", ctx))
    )

    order_type = normalize_order_type(require_str(data, "ordertype", ctx))

    return Fill(
        execId=txid,
        orderId=require_str(data, "ordertxid", ctx),
        symbol=require_str(data, "pair", ctx),
        assetClass="crypto",
        side=side,
        orderType=order_type,
        price=require_float(data, "price", ctx),
        volume=require_float(data, "vol", ctx),
        cost=require_float(data, "cost", ctx),
        fee=abs(require_float(data, "fee", ctx)),
        timestamp=ts,
        source="rest_poll",
        raw={"txid": txid, **data},
    )


def _build_fetch(client: KrakenClient) -> Callable[[], str | None]:
    """Return a fetch callable for the generic poller engine.

    Returns a JSON string of the raw trades dict, or None on failure.
    """

    def fetch() -> str | None:
        try:
            all_trades: dict[str, KrakenRestTrade] = {}
            offset = 0
            total_count: int | None = None

            while True:
                result = client.get_trades_history(ofs=offset)
                trades_raw = result.get("trades", {})
                if not isinstance(trades_raw, dict):
                    raise ValueError(
                        f"Invalid Kraken trades history response at ofs={offset}: "
                        f"'trades' must be a dict, got {type(trades_raw).__name__}"
                    )

                count_raw = result.get("count", 0)
                try:
                    page_total_count = int(count_raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Invalid Kraken trades history response at ofs={offset}: "
                        f"'count' must be an integer, got {count_raw!r}"
                    ) from exc

                if total_count is None:
                    total_count = page_total_count

                page_trades = cast(dict[str, KrakenRestTrade], trades_raw)
                if not page_trades:
                    break

                all_trades.update(page_trades)
                offset += len(page_trades)

                if offset >= page_total_count:
                    break

            return json.dumps(
                {
                    "trades": all_trades,
                    "count": total_count if total_count is not None else len(all_trades),
                }
            )
        except Exception:
            log.exception("Failed to fetch trades from Kraken")
            return None

    return fetch


def _build_parse() -> Callable[[str], tuple[list[Fill], list[str]]]:
    """Return a parse callable for the generic poller engine."""

    def parse(raw: str) -> tuple[list[Fill], list[str]]:
        try:
            result: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            return [], [f"Failed to parse Kraken REST response JSON: {exc}"]

        if not isinstance(result, dict):
            return [], [
                "Failed to parse Kraken REST response: top-level JSON must be an object"
            ]

        raw_trades = result.get("trades", {})

        if not isinstance(raw_trades, dict):
            return [], [
                "Failed to parse Kraken REST response: 'trades' must be an object"
            ]

        fills: list[Fill] = []
        errors: list[str] = []

        for txid, trade_data in raw_trades.items():
            if not isinstance(trade_data, dict):
                errors.append(f"Failed to parse trade {txid}: expected an object, got {type(trade_data).__name__}")
                continue
            try:
                fill = _parse_rest_trade(txid, cast(KrakenRestTrade, trade_data))
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


def _build_connect(client: KrakenClient) -> Callable[[aiohttp.ClientSession], Awaitable[aiohttp.ClientWebSocketResponse]]:
    """Build a connect callback that obtains a WS token, connects, and subscribes."""

    async def connect(
        session: aiohttp.ClientSession,
    ) -> aiohttp.ClientWebSocketResponse:
        # Obtain short-lived WS token via REST API (blocking, run in thread)
        import asyncio
        try:
            token = await asyncio.to_thread(client.get_ws_token)
        except RuntimeError as exc:
            if "Permission denied" in str(exc):
                raise FatalListenerError(
                    f"Kraken API key lacks permission — check key scopes: {exc}"
                ) from exc
            raise

        ws = await session.ws_connect(_WS_URL, heartbeat=30.0)

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
