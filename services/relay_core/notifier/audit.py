"""Fill audit logger — rotating JSONL file with 30-day retention.

Writes two event types:
  fills_received  — individual fills before dedup/aggregation (listener + poller)
  webhook_sent    — the aggregated WebhookPayloadTrades just before dispatch

Enabled automatically when /data/logs/ is writable; disabled with a
warning when the directory or log file cannot be created (e.g. unit-test
environments or read-only mounts).

Override the log path via FILL_AUDIT_LOG_PATH env var. In production the
default directory is the ``relay-logs`` named volume, so the history
survives container recreation (every deploy).
"""

import functools
import json
import logging
import logging.handlers
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from relay_core.env import get_env
from shared import Fill

_module_log = logging.getLogger(__name__)

_DEFAULT_PATH = "/data/logs/fills_audit.jsonl"
# Daily files kept after rotation — the audit history spans ~30 days.
_BACKUP_COUNT = 30


def _get_path() -> str:
    return get_env("FILL_AUDIT_LOG_PATH", default=_DEFAULT_PATH)


@functools.cache
def _init() -> logging.Logger | None:
    """Initialise and return the rotating audit logger (called at most once).

    Returns None when the log directory cannot be created — audit logging
    is disabled for the process lifetime in that case.
    """
    path = Path(_get_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _module_log.warning(
            "Fill audit log disabled — cannot create %s: %s", path.parent, exc,
        )
        return None

    logger = logging.getLogger("relay_core.fill_audit")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        try:
            handler = logging.handlers.TimedRotatingFileHandler(
                str(path),
                when="midnight",
                interval=1,
                backupCount=_BACKUP_COUNT,
                utc=True,
            )
        except OSError as exc:
            _module_log.warning(
                "Fill audit log disabled — cannot open %s: %s", path, exc,
            )
            return None
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        _module_log.info("Fill audit log: %s (%d-day retention)", path, _BACKUP_COUNT)

    return logger


def _write(entry: dict[str, Any]) -> None:
    logger = _init()
    if logger is None:
        return
    try:
        logger.debug(json.dumps(entry, default=str, separators=(",", ":")))
    except Exception:
        _module_log.exception("Failed to write fill audit log entry")


def log_fills(relay_name: str, fills: list[Fill]) -> None:
    """Log individual fills before dedup and aggregation.

    Called from both listener (_send_and_mark / _send_no_mark) and
    poller (poll_once) just before the dedup step, so the entry captures
    every fill the engine received — including any that will be dropped
    as duplicates.
    """
    _write({
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "relay": relay_name,
        "event": "fills_received",
        "count": len(fills),
        "fills": [
            {
                "execId": f.execId,
                "orderId": f.orderId,
                "symbol": f.symbol,
                "side": f.side.value,
                "volume": f.volume,
                "price": f.price,
                "cost": f.cost,
                "fee": f.fee,
                "timestamp": f.timestamp,
                "source": f.source,
            }
            for f in fills
        ],
    })


def log_payload(relay_name: str | None, payload: BaseModel) -> None:
    """Log the outbound webhook payload (aggregated trades) before dispatch.

    The ``raw`` field is stripped from nested Trade objects — it can be
    large and rarely adds diagnostic value for volume/price discrepancies.
    """
    try:
        d = payload.model_dump()
        for trade in d.get("data", []):
            trade.pop("raw", None)
        data: dict[str, Any] = d
    except Exception:
        data = {"error": repr(payload)}
    _write({
        "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "relay": relay_name or "unknown",
        "event": "webhook_sent",
        "payload": data,
    })
