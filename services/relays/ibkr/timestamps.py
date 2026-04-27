"""IBKR-specific timestamp → ISO-8601 conversion.

Two native formats arrive from IBKR:

* **Flex XML** (``dateTime`` attribute): ``YYYYMMDD;HHMMSS`` e.g. ``20250403;153000``
* **ib_async bridge** (``Execution.time``): ISO-8601 e.g. ``2026-04-22T15:31:28+00:00``

Flex timestamps are naive. Bridge timestamps may carry a UTC offset when
ib_async returns a timezone-aware ``datetime``. Each function here turns one
format into an ISO-8601 string (possibly tz-aware) that the shared
:func:`shared.normalize_timestamp` layer can finish — converting to canonical
UTC and applying ``IBKR_ACCOUNT_TIMEZONE`` for naive inputs.

Having the format knowledge here (rather than in ``shared/time_format.py``)
keeps ``time_format`` broker-agnostic — every new relay's quirks stay in
its own package.
"""

from datetime import datetime


def flex_to_iso(raw: str) -> str:
    """Convert a Flex XML ``dateTime`` attribute to naive ISO-8601.

    Raises ``ValueError`` when *raw* does not match the expected form.
    """
    try:
        dt = datetime.strptime(raw, "%Y%m%d;%H%M%S")
    except ValueError as exc:
        raise ValueError(
            f"Not a valid IBKR Flex dateTime (expected YYYYMMDD;HHMMSS): {raw!r}"
        ) from exc
    return dt.isoformat(timespec="seconds")


def flex_date_to_iso(raw: str) -> str:
    """Normalise an IBKR option-expiry date to ISO ``YYYY-MM-DD``.

    Used for option ``expiry`` (Flex) and ``lastTradeDateOrContractMonth``
    (ib_async). Accepts two input shapes:

    * **Compact** ``YYYYMMDD`` (current wire format) — converted to ISO.
    * **ISO** ``YYYY-MM-DD`` — passed through as-is. Defensive: if IBKR
      ever flips the wire format on us, the helper keeps working without
      a code change.

    Strict otherwise — typos and partial-format inputs raise
    ``ValueError`` so they can't sneak through as semantically
    meaningful but wrong dates. The explicit length + ``isdigit``
    guards exist because Python's ``strptime("%Y%m%d")`` is greedy on
    ``%Y`` and silently accepts 7-character inputs like ``"2026508"``
    as year=2026, month=5, day=08.
    """
    # ISO YYYY-MM-DD: validate and forward unchanged.
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(
                f"Not a valid ISO date (expected YYYY-MM-DD): {raw!r}"
            ) from exc
        return raw

    # Compact YYYYMMDD: convert to ISO.
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(
            f"Not a valid IBKR date (expected YYYYMMDD or YYYY-MM-DD): {raw!r}"
        )
    try:
        dt = datetime.strptime(raw, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(
            f"Not a valid IBKR compact date (expected YYYYMMDD): {raw!r}"
        ) from exc
    return dt.date().isoformat()


def bridge_to_iso(raw: str) -> str:
    """Convert an ib_async bridge ``Execution.time`` to ISO-8601.

    Accepts two formats:

    * **ISO-8601** (current): ``YYYY-MM-DDTHH:MM:SS[±HH:MM|Z]`` — passed through
      as-is so ``normalize_timestamp`` handles timezone conversion downstream.
    * **Legacy** ``YYYYMMDD-HH:MM:SS`` — converted to naive ISO-8601.

    Raises ``ValueError`` when *raw* is empty or matches neither form.
    """
    if not raw:
        raise ValueError("Not a valid IBKR bridge time: empty string")
    # Distinguish by structure: ISO-8601 has a '-' at position 4 (YYYY-…),
    # while the legacy format starts with 8 compact digits (YYYYMMDD…).
    if len(raw) > 4 and raw[4] == "-":
        try:
            datetime.fromisoformat(raw)
            return raw
        except ValueError as exc:
            raise ValueError(
                f"Not a valid IBKR bridge time (expected ISO-8601): {raw!r}"
            ) from exc
    try:
        dt = datetime.strptime(raw, "%Y%m%d-%H:%M:%S")
        return dt.isoformat(timespec="seconds")
    except ValueError as exc:
        raise ValueError(
            f"Not a valid IBKR bridge time (expected ISO-8601 or YYYYMMDD-HH:MM:SS): {raw!r}"
        ) from exc
