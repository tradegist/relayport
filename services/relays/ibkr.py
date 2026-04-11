"""IBKR relay adapter — single-file broker integration.

Wires IBKR Flex polling and ibkr_bridge WebSocket listening into the
generic ``relay_core`` engines.  All IBKR-specific logic lives here:
env var getters, Flex fetch, XML parsing, WS envelope mapping.
"""

import logging
import os
from collections.abc import Callable
from typing import Any, cast

from listener.bridge_models import WsEnvelope
from notifier.base import BaseNotifier
from poller.flex_parser import parse_fills
from relay_core import (
    BrokerRelay,
    FillHandler,
    ListenerConfig,
    PollerConfig,
    get_debounce_ms,
    get_poll_interval,
    is_listener_enabled,
    is_poller_enabled,
)
from shared import BuySell, Fill, Source, normalize_asset_class

log = logging.getLogger("relays.ibkr")


# ── Env var getters (IBKR-specific) ─────────────────────────────────

def _get_flex_token(suffix: str = "") -> str | None:
    key = f"IBKR_FLEX_TOKEN{suffix}"
    return os.environ.get(key, "").strip() or None


def _get_flex_query_id(suffix: str = "") -> str | None:
    key = f"IBKR_FLEX_QUERY_ID{suffix}"
    return os.environ.get(key, "").strip() or None


def _get_bridge_ws_url() -> str:
    key = "IBKR_BRIDGE_WS_URL"
    val = os.environ.get(key, "").strip()
    if not val:
        # Fall back to legacy name
        val = os.environ.get("BRIDGE_WS_URL", "").strip()
    if not val:
        raise SystemExit(f"{key} (or BRIDGE_WS_URL) must be set")
    return val


def _get_bridge_api_token() -> str:
    key = "IBKR_BRIDGE_API_TOKEN"
    val = os.environ.get(key, "").strip()
    if not val:
        val = os.environ.get("BRIDGE_API_TOKEN", "").strip()
    if not val:
        raise SystemExit(f"{key} (or BRIDGE_API_TOKEN) must be set")
    return val


def _is_exec_events_enabled() -> bool:
    val = os.environ.get("IBKR_LISTENER_EXEC_EVENTS_ENABLED", "").strip().lower()
    if not val:
        val = os.environ.get("LISTENER_EXEC_EVENTS_ENABLED", "false").strip().lower()
    return val not in ("0", "false", "no", "")


# ── Flex poller adapter ──────────────────────────────────────────────

def _build_fetch(flex_token: str, flex_query_id: str) -> Callable[[], str | None]:
    """Return a fetch callable for the generic poller engine."""
    from poller import fetch_flex_report

    def fetch() -> str | None:
        return fetch_flex_report(
            flex_token=flex_token, flex_query_id=flex_query_id,
        )

    return fetch


def _build_poller_configs() -> list[PollerConfig]:
    """Build PollerConfig(s) from env vars.

    Detects IBKR_FLEX_QUERY_ID_2 etc. for multi-account support.
    Returns an empty list when polling is disabled or no Flex
    credentials are configured (listener-only mode).
    """
    if not is_poller_enabled("ibkr"):
        return []

    configs: list[PollerConfig] = []
    interval = get_poll_interval("ibkr")

    # Primary poller — optional (both must be set, or both unset)
    token = _get_flex_token()
    query_id = _get_flex_query_id()
    if token and query_id:
        configs.append(PollerConfig(
            fetch=_build_fetch(token, query_id),
            parse=parse_fills,
            interval=interval,
        ))
    elif token or query_id:
        missing = "IBKR_FLEX_QUERY_ID" if token else "IBKR_FLEX_TOKEN"
        raise SystemExit(
            f"IBKR poller partially configured — {missing} must be set"
        )

    # Secondary poller (_2 suffix) — optional
    token_2 = _get_flex_token("_2")
    query_2 = _get_flex_query_id("_2")
    if token_2 and query_2:
        configs.append(PollerConfig(
            fetch=_build_fetch(token_2, query_2),
            parse=parse_fills,
            interval=interval,
        ))

    return configs


# ── Bridge WS listener adapter ──────────────────────────────────────

# Side mapping (financial enum — never assume a default)
_SIDE_MAP: dict[str, BuySell] = {
    "BOT": BuySell.BUY,
    "SLD": BuySell.SELL,
}


def _map_fill(envelope: WsEnvelope) -> Fill | None:
    """Map a WsEnvelope with fill data to a relay Fill model.

    Returns None and logs an error if:
    - The envelope has no fill data (status events).
    - The execution side is not ``"BOT"`` or ``"SLD"``.
    """
    if envelope.fill is None:
        log.error(
            "Envelope seq=%d type=%s has no fill data",
            envelope.seq, envelope.type,
        )
        return None

    ex = envelope.fill.execution
    contract = envelope.fill.contract
    cr = envelope.fill.commissionReport

    exec_id = ex.execId.strip()
    if not exec_id:
        log.error(
            "Empty execId for envelope seq=%d type=%s symbol=%s — skipping fill",
            envelope.seq, envelope.type, contract.symbol,
        )
        return None

    # Financial enum — never assume a default for buy/sell side.
    side = _SIDE_MAP.get(ex.side)
    if side is None:
        log.error(
            "Unknown execution side %r for execId=%s — skipping fill",
            ex.side, exec_id,
        )
        return None

    source = cast(Source, envelope.type)

    return Fill(
        execId=exec_id,
        orderId=str(ex.permId),
        symbol=contract.symbol,
        assetClass=normalize_asset_class(contract.secType),
        side=side,
        orderType=None,  # WS events don't carry order type info
        price=ex.price,
        volume=ex.shares,
        cost=ex.price * ex.shares,
        fee=abs(cr.commission),  # Always positive (amount paid)
        timestamp=ex.time,
        source=source,
        raw=envelope.model_dump(),
    )


def _event_filter(data: dict[str, Any]) -> bool:
    """Return True for events the IBKR adapter handles."""
    event_type = data.get("type")

    # Status events — log only
    if event_type in ("connected", "disconnected"):
        log.info("Bridge status: %s", event_type)
        return False

    if event_type not in ("execDetailsEvent", "commissionReportEvent"):
        log.warning("Unrecognized event type: %s", event_type)
        return False

    return True


def _on_message_factory(
    exec_events_enabled: bool,
) -> Any:
    """Build an on_message callback with exec_events_enabled baked in."""
    async def handler(
        data: dict[str, Any],
        send_and_mark: FillHandler,
        send_no_mark: FillHandler,
    ) -> None:
        event_type = data.get("type")

        try:
            envelope = WsEnvelope.model_validate(data)
        except Exception:
            log.exception("Failed to validate WsEnvelope (type=%s)", event_type)
            return

        fill = _map_fill(envelope)
        if fill is None:
            return

        if envelope.type == "execDetailsEvent":
            if not exec_events_enabled:
                log.debug("Skipping execDetailsEvent (disabled)")
                return
            await send_no_mark(fill)
        else:
            # commissionReportEvent — full dedup pipeline
            await send_and_mark(fill)

    return handler


def _build_listener_config() -> ListenerConfig | None:
    """Build ListenerConfig if listener is enabled, else return None."""
    if not is_listener_enabled("ibkr"):
        return None

    exec_events_enabled = _is_exec_events_enabled()

    return ListenerConfig(
        ws_url=_get_bridge_ws_url(),
        api_token=_get_bridge_api_token(),
        on_message=_on_message_factory(exec_events_enabled),
        event_filter=_event_filter,
        debounce_ms=get_debounce_ms("ibkr"),
    )


# ── Public API ───────────────────────────────────────────────────────

def build_relay(notifiers: list[BaseNotifier]) -> BrokerRelay:
    """Build a fully configured IBKR relay instance."""
    poller_configs = _build_poller_configs()
    listener_config = _build_listener_config()

    if not poller_configs and listener_config is None:
        raise SystemExit(
            "IBKR relay has neither poller nor listener configured. "
            "Set IBKR_FLEX_TOKEN + IBKR_FLEX_QUERY_ID for polling, "
            "or IBKR_LISTENER_ENABLED=true for real-time events."
        )

    if not poller_configs:
        log.info("IBKR: listener-only mode (no Flex credentials)")
    if listener_config is None:
        log.info("IBKR: poller-only mode (listener disabled)")

    return BrokerRelay(
        name="ibkr",
        notifiers=notifiers,
        poller_configs=poller_configs,
        listener_config=listener_config,
    )
