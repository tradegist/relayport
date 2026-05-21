"""Shared validation helpers for market_data request models."""


def parse_string_list(v: object, *, max_count: int | None = None) -> list[str]:
    """Parse a comma-separated string or list into a deduplicated, uppercased list."""
    if isinstance(v, str):
        items = [s.strip().upper() for s in v.split(",") if s.strip()]
    elif isinstance(v, list):
        items = [str(s).strip().upper() for s in v if str(s).strip()]
    else:
        raise ValueError("must be a comma-separated string or list")
    if not items:
        raise ValueError("must contain at least one non-empty value")
    items = list(dict.fromkeys(items))
    if max_count is not None and len(items) > max_count:
        raise ValueError(f"list exceeds maximum of {max_count} items")
    return items
