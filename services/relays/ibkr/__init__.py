"""IBKR relay adapter — single-file broker integration.

Wires IBKR Flex polling and ibkr_bridge WebSocket listening into the
generic ``relay_core`` engines.  All IBKR-specific logic lives here:
env var getters, Flex fetch, XML parsing, WS envelope mapping.
"""

import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import aiohttp
from pydantic import ValidationError

from relay_core import (
    BaseNotifier,
    BrokerRelay,
    ListenerConfig,
    OnMessageResult,
    PollerConfig,
    StartupContext,
    get_debounce_ms,
    get_poll_interval,
    is_listener_enabled,
    is_poller_enabled,
)
from shared import (
    BuySell,
    Fill,
    OptionContract,
    Source,
    normalize_timestamp,
    parse_timezone,
)

from .bridge_models import WsContract, WsEnvelope
from .flex_fetch import RedactTokenFilter, fetch_flex_report
from .flex_parser import parse_fills
from .timestamps import bridge_to_iso, flex_date_to_iso
from .utilities import normalize_asset_class

log = logging.getLogger("relays.ibkr")


# ── Env var getters (IBKR-specific) ─────────────────────────────────

def _get_flex_token(suffix: str = "") -> str | None:
    key = f"IBKR_FLEX_TOKEN{suffix}"
    return os.environ.get(key, "").strip() or None


def _get_flex_query_id(suffix: str = "") -> str | None:
    key = f"IBKR_FLEX_QUERY_ID{suffix}"
    return os.environ.get(key, "").strip() or None


def _get_flex_lookback_days() -> int | None:
    """Optional override for the Flex query's saved Period.

    When set, ``SendRequest`` is called with the documented ``p`` URL
    param so IBKR returns the last N calendar days regardless of how
    the query is configured server-side.  IBKR caps the override at
    365 days; values outside ``[1, 365]`` raise ``SystemExit`` at boot.

    Returning ``None`` (var unset) lets the saved query Period apply.
    """
    raw = os.environ.get("IBKR_FLEX_LOOKBACK_DAYS", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(
            f"Invalid IBKR_FLEX_LOOKBACK_DAYS={raw!r} — must be an integer"
        ) from None
    if value < 1 or value > 365:
        raise SystemExit(
            f"Invalid IBKR_FLEX_LOOKBACK_DAYS={raw!r} — must be between 1 and 365"
        )
    return value


def _get_bridge_ws_url() -> str:
    key = "IBKR_BRIDGE_WS_URL"
    val = os.environ.get(key, "").strip()
    if not val:
        raise SystemExit(f"{key} must be set")
    return val


def _get_bridge_api_token() -> str:
    key = "IBKR_BRIDGE_API_TOKEN"
    val = os.environ.get(key, "").strip()
    if not val:
        raise SystemExit(f"{key} must be set")
    return val


def _is_exec_events_enabled() -> bool:
    val = os.environ.get("IBKR_LISTENER_EXEC_EVENTS_ENABLED", "false").strip().lower()
    return val not in ("0", "false", "no", "")


def _get_account_timezone() -> ZoneInfo:
    """Return the IANA tz for IBKR timestamps (defaults to UTC).

    IBKR reports every trade timestamp (both Flex XML and ib_async bridge
    events) in the account's base timezone with no tz label. Without
    this hint, naive timestamps get treated as UTC — fine if the account
    is actually UTC, wrong otherwise. Setting ``IBKR_ACCOUNT_TIMEZONE``
    to a valid IANA zone (e.g. ``America/New_York``) makes the engine
    convert the broker's local time to UTC before storing.

    Validated fail-fast at boot: an invalid value raises ``SystemExit``.
    """
    raw = os.environ.get("IBKR_ACCOUNT_TIMEZONE", "").strip()
    if not raw:
        return ZoneInfo("UTC")
    try:
        return parse_timezone(raw)
    except ValueError as exc:
        raise SystemExit(
            f"Invalid IBKR_ACCOUNT_TIMEZONE={raw!r} — must be a valid IANA"
            f" timezone (e.g. America/New_York, Europe/Zurich, UTC): {exc}"
        ) from None


# ── Flex poller adapter ──────────────────────────────────────────────

def _build_fetch(
    flex_token: str, flex_query_id: str, lookback_days: int | None,
) -> Callable[[], str | None]:
    """Return a fetch callable for the generic poller engine."""

    def fetch() -> str | None:
        return fetch_flex_report(
            flex_token=flex_token,
            flex_query_id=flex_query_id,
            lookback_days=lookback_days,
        )

    return fetch


def _build_parse(tz: ZoneInfo) -> Callable[[str], tuple[list[Fill], list[str]]]:
    """Bind the IBKR account timezone into the Flex parser callback."""
    def parse(xml: str) -> tuple[list[Fill], list[str]]:
        return parse_fills(xml, tz=tz)
    return parse


def _build_poller_configs(tz: ZoneInfo) -> list[PollerConfig]:
    """Build PollerConfig(s) from env vars.

    Detects IBKR_FLEX_QUERY_ID_2 etc. for multi-account support.
    Returns an empty list when polling is disabled or no Flex
    credentials are configured (listener-only mode).
    """
    if not is_poller_enabled("ibkr"):
        return []

    configs: list[PollerConfig] = []
    interval = get_poll_interval("ibkr")
    if interval < 420:
        log.warning(
            "IBKR poll interval is %ds — IBKR's Flex Web Service is limited to"
            " 10 requests per minute per token (shared across query IDs). Values"
            " below 420s (7 min) leave little headroom for retries and risk"
            " ErrorCode 1018 (too many requests).",
            interval,
        )
    parse = _build_parse(tz)
    lookback_days = _get_flex_lookback_days()

    # Primary poller — optional (both must be set, or both unset)
    token = _get_flex_token()
    query_id = _get_flex_query_id()
    if token and query_id:
        configs.append(PollerConfig(
            fetch=_build_fetch(token, query_id, lookback_days),
            parse=parse,
            interval=interval,
        ))
    elif token or query_id:
        missing = "IBKR_FLEX_QUERY_ID" if token else "IBKR_FLEX_TOKEN"
        raise SystemExit(
            f"IBKR poller partially configured — {missing} must be set"
        )

    # Secondary poller (_2 suffix) — only IBKR_FLEX_QUERY_ID_2 is required;
    # IBKR_FLEX_TOKEN_2 falls back to the primary token.
    query_2 = _get_flex_query_id("_2")
    if query_2:
        token_2 = _get_flex_token("_2") or token
        if not token_2:
            raise SystemExit(
                "IBKR_FLEX_QUERY_ID_2 is set but no token available"
                " — set IBKR_FLEX_TOKEN_2 or IBKR_FLEX_TOKEN"
            )
        configs.append(PollerConfig(
            fetch=_build_fetch(token_2, query_2, lookback_days),
            parse=parse,
            interval=interval,
        ))

    return configs


# ── Bridge WS listener adapter ──────────────────────────────────────

# Side mapping (financial enum — never assume a default)
_SIDE_MAP: dict[str, BuySell] = {
    "BOT": BuySell.BUY,
    "SLD": BuySell.SELL,
}


def _map_fill(envelope: WsEnvelope, tz: ZoneInfo) -> Fill:
    """Map a WsEnvelope with fill data to a relay Fill model.

    Raises ``ValueError`` describing why the fill was skipped if:
    - The envelope has no fill data.
    - The execution side is not ``"BOT"`` or ``"SLD"``.
    - The execution time cannot be parsed.

    *tz* is the IANA timezone to interpret IBKR's naive timestamps in.
    """
    if envelope.fill is None:
        raise ValueError(
            f"WsEnvelope seq={envelope.seq} type={envelope.type!r} has no fill data"
        )

    ex = envelope.fill.execution
    contract = envelope.fill.contract
    cr = envelope.fill.commissionReport

    exec_id = ex.execId.strip()
    if not exec_id:
        raise ValueError(
            f"Empty execId in envelope seq={envelope.seq} type={envelope.type!r}"
            f" symbol={contract.symbol!r}"
        )

    # Financial enum — never assume a default for buy/sell side.
    side = _SIDE_MAP.get(ex.side)
    if side is None:
        raise ValueError(
            f"Unknown execution side {ex.side!r} for execId={exec_id!r}"
        )

    try:
        ts = normalize_timestamp(bridge_to_iso(ex.time), assume_tz=tz)
    except ValueError as exc:
        raise ValueError(
            f"Bad execution time {ex.time!r} for execId={exec_id!r}: {exc}"
        ) from exc

    source = cast(Source, envelope.type)
    currency = contract.currency.strip().upper() or None
    asset_class = normalize_asset_class(contract.secType)

    # symbol / option resolution.  ib_async populates Contract.symbol with
    # the underlying ticker for every secType (so for OPT it's e.g. "TSLA",
    # not the option contract).  The OCC option ticker lives on
    # Contract.localSymbol — IBKR pads the underlying to 6 chars with spaces
    # (e.g. "TSLA  281215C00350000"). Spaces are stripped so the symbol is
    # URL-friendly (e.g. "TSLA281215C00350000").
    # Mirror Flex's convention: Fill.symbol = full instrument identifier,
    # Fill.option = nested OptionContract for derivatives, None otherwise.
    option: OptionContract | None
    if asset_class == "option":
        symbol = contract.localSymbol.strip().replace(" ", "")
        if not symbol:
            raise ValueError(
                f"Empty localSymbol for option execId={exec_id!r}"
                f" underlying={contract.symbol!r} — cannot identify the contract"
            )
        option = _build_option_contract(contract, exec_id)
    else:
        symbol = contract.symbol
        option = None

    return Fill(
        execId=exec_id,
        orderId=str(ex.permId),
        symbol=symbol,
        assetClass=asset_class,
        side=side,
        orderType=None,  # WS events don't carry order type info
        price=ex.price,
        volume=ex.shares,
        cost=ex.price * ex.shares,
        fee=abs(cr.commission),  # Always positive (amount paid)
        timestamp=ts,
        source=source,
        currency=currency,
        option=option,
        raw=envelope.model_dump(),
    )


# Bridge ``Contract.right`` values → OptionContract.type literals.
# ib_async accepts either single-letter or spelled-out forms.
_OPT_RIGHT_MAP: dict[str, Literal["call", "put"]] = {
    "C": "call",
    "CALL": "call",
    "P": "put",
    "PUT": "put",
}


def _build_option_contract(contract: WsContract, exec_id: str) -> OptionContract:
    """Assemble an :class:`OptionContract` from a bridge ``WsContract``.

    Raises :class:`ValueError` (caller maps this to an OnMessageResult error)
    when any required option field is missing or invalid — emitting an
    option fill with incomplete metadata produces a webhook payload that
    consumers can't reliably interpret.
    """
    root_symbol = contract.symbol.strip()
    if not root_symbol:
        raise ValueError(
            f"Empty Contract.symbol on option execId={exec_id!r}"
            f" — cannot identify the underlying"
        )

    if contract.strike <= 0:
        raise ValueError(
            f"Non-positive Contract.strike {contract.strike!r} on option"
            f" execId={exec_id!r}"
        )

    expiry_raw = contract.lastTradeDateOrContractMonth.strip()
    if not expiry_raw:
        raise ValueError(
            f"Empty lastTradeDateOrContractMonth on option execId={exec_id!r}"
        )
    try:
        expiry_iso = flex_date_to_iso(expiry_raw)
    except ValueError as exc:
        raise ValueError(
            f"Bad lastTradeDateOrContractMonth {expiry_raw!r} on option"
            f" execId={exec_id!r}: {exc}"
        ) from exc

    right_upper = contract.right.strip().upper()
    opt_type = _OPT_RIGHT_MAP.get(right_upper)
    if opt_type is None:
        raise ValueError(
            f"Unknown Contract.right {contract.right!r} on option"
            f" execId={exec_id!r}"
        )

    return OptionContract(
        rootSymbol=root_symbol,
        strike=contract.strike,
        expiryDate=expiry_iso,
        type=opt_type,
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
    exec_events_enabled: bool, tz: ZoneInfo,
) -> Callable[[dict[str, Any]], Awaitable[list[OnMessageResult]]]:
    """Build an on_message callback with exec_events_enabled baked in."""
    async def handler(
        data: dict[str, Any],
    ) -> list[OnMessageResult]:
        event_type = data.get("type")

        try:
            envelope = WsEnvelope.model_validate(data)
        except ValidationError as exc:
            sanitized = "; ".join(
                f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}"
                for e in exc.errors(include_input=False, include_url=False)
            )
            log.error("Failed to validate IBKR WsEnvelope (type=%r): %s", event_type, sanitized)
            return [OnMessageResult(error=f"Invalid IBKR envelope (type={event_type!r})")]

        try:
            fill = _map_fill(envelope, tz)
        except ValueError as exc:
            return [OnMessageResult(error=str(exc))]

        if envelope.type == "execDetailsEvent":
            if not exec_events_enabled:
                log.debug("Skipping execDetailsEvent (disabled)")
                return []
            return [OnMessageResult(fill=fill, mark=False)]

        # commissionReportEvent — full dedup pipeline
        return [OnMessageResult(fill=fill, mark=True)]

    return handler


def _build_connect(
    ws_url: str, api_token: str,
) -> Callable[[aiohttp.ClientSession], Awaitable[aiohttp.ClientWebSocketResponse]]:
    """Build a connect callback that opens an authenticated WS connection.

    Tracks ``last_seq`` across reconnects so the bridge can resume from
    the last seen sequence number.
    """
    state = {"last_seq": 0}

    async def connect(
        session: aiohttp.ClientSession,
    ) -> aiohttp.ClientWebSocketResponse:
        url = ws_url
        if state["last_seq"] > 0:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}last_seq={state['last_seq']}"

        headers = {"Authorization": f"Bearer {api_token}"}
        log.debug("[ibkr] WS URL: %s", url)
        ws = await session.ws_connect(url, headers=headers, heartbeat=30.0)

        # Wrap the original receive method to track seq numbers.
        _orig_receive = ws.receive

        async def _tracking_receive() -> aiohttp.WSMessage:
            msg = await _orig_receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                import json
                try:
                    data = json.loads(msg.data)
                    seq = data.get("seq")
                    if isinstance(seq, int):
                        state["last_seq"] = seq
                except (ValueError, TypeError) as exc:
                    log.debug("[ibkr] Could not parse seq from WS message: %s", exc)
            return msg

        # `receive` is a regular async method on ClientWebSocketResponse (no
        # __slots__), so attribute assignment is safe at runtime.  We patch at
        # this level — rather than inside on_message — so that seq is tracked
        # for every incoming WS message, including status events
        # ("connected"/"disconnected") that event_filter discards before
        # on_message is invoked.
        ws.receive = _tracking_receive  # type: ignore[assignment] # aiohttp stubs mark receive as non-assignable; runtime monkey-patch is intentional
        return ws

    return connect


def _build_listener_config(tz: ZoneInfo) -> ListenerConfig | None:
    """Build ListenerConfig if listener is enabled, else return None."""
    if not is_listener_enabled("ibkr"):
        return None

    exec_events_enabled = _is_exec_events_enabled()

    return ListenerConfig(
        connect=_build_connect(_get_bridge_ws_url(), _get_bridge_api_token()),
        on_message=_on_message_factory(exec_events_enabled, tz),
        event_filter=_event_filter,
        debounce_ms=get_debounce_ms("ibkr"),
    )


# ── Startup lifecycle ────────────────────────────────────────────────


def _on_start(ctx: StartupContext) -> None:
    ctx.add_logging_filter(RedactTokenFilter())


# ── Public API ───────────────────────────────────────────────────────

def build_relay(notifiers: list[BaseNotifier]) -> BrokerRelay:
    """Build a fully configured IBKR relay instance."""
    # Validate IBKR_ACCOUNT_TIMEZONE fail-fast at boot — if the user set
    # a malformed value we want the container to exit immediately rather
    # than silently defaulting or failing per-fill.
    tz = _get_account_timezone()
    log.info("IBKR account timezone: %s", tz.key)

    poller_configs = _build_poller_configs(tz)
    listener_config = _build_listener_config(tz)

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
        on_start=_on_start,
    )
