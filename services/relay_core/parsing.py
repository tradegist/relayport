"""Shared parsing helpers for relay adapters.

Relay adapters convert broker-specific data (REST JSON, WebSocket messages,
XML) into ``Fill`` objects.  These helpers enforce that required fields are
present and well-typed, raising ``ValueError`` on missing or invalid data so
that the caller can record the error instead of emitting a bogus fill.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def require_str(data: Mapping[str, Any], key: str, ctx: str) -> str:
    """Return a non-empty string value for *key*, or raise ``ValueError``."""
    val = data.get(key)
    if val is None:
        raise ValueError(f"{ctx}: missing required field {key!r}")
    result = str(val).strip()
    if not result:
        raise ValueError(f"{ctx}: empty required field {key!r}")
    return result


def require_float(data: Mapping[str, Any], key: str, ctx: str) -> float:
    """Return a float value for *key*, or raise ``ValueError``."""
    val = data.get(key)
    if val is None:
        raise ValueError(f"{ctx}: missing required field {key!r}")
    try:
        return float(val)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{ctx}: invalid numeric field {key!r}: {val!r}"
        ) from exc
