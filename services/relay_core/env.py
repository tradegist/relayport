"""Env var helpers with relay-specific prefix fallback.

All relay-core env var readers follow the same pattern: try
``{prefix}{VAR}{suffix}`` first, fall back to ``{VAR}{suffix}``.
This module centralises that logic so callers don't duplicate it.
"""

import os


def get_env(var: str, prefix: str = "", suffix: str = "", default: str = "") -> str:
    """Read a string env var with prefix fallback.

    Resolution order:
        1. ``{prefix}{var}{suffix}``  (relay-specific)
        2. ``{var}{suffix}``          (generic fallback)
        3. *default*                  (when neither is set)
    """
    if prefix:
        val = os.environ.get(f"{prefix}{var}{suffix}", "").strip()
        if val:
            return val
    return os.environ.get(f"{var}{suffix}", "").strip() or default


def get_env_int(
    var: str, prefix: str = "", suffix: str = "", default: str = "0",
) -> tuple[str, int]:
    """Read an integer env var with prefix fallback.

    Returns ``(resolved_var_name, value)`` so callers can include the
    actual var name in error messages.

    Raises:
        SystemExit: When the value is not a valid integer.
    """
    prefixed = f"{prefix}{var}{suffix}"
    generic = f"{var}{suffix}"
    if prefix:
        raw = os.environ.get(prefixed, "").strip()
        if raw:
            name = prefixed
        else:
            name = generic
            raw = os.environ.get(generic, default).strip()
    else:
        name = generic
        raw = os.environ.get(generic, default).strip()
    try:
        return name, int(raw)
    except ValueError:
        raise SystemExit(f"Invalid {name}={raw!r} — must be an integer") from None
