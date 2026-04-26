"""Sanitize a raw IBKR Flex XML dump into a committable test fixture.

Usage:
    python services/relays/ibkr/fixtures/sanitize.py INPUT.xml OUTPUT.xml

Replaces identifying attribute values (account, order, execution, transaction
IDs) with synthetic values.  Market data (symbol, conid, ISIN, CUSIP, FIGI,
exchange) is public and kept as-is.  Prices/quantities/P&L are kept to
preserve realistic arithmetic in tests.

Three classes of sanitization:

* **Static** attrs (``accountId``, ``acctAlias``, ``model``, origin/related
  IDs) get a single constant that is identical across every row — these are
  account-level facts that don't vary between fills.

* **Order-level** attrs (``ibOrderID``/``orderID``) are replaced via a
  document-order mapping: the first distinct order ID seen becomes
  ``333333331``, the second ``333333332``, etc.  An ``<Order>`` summary
  row and all its ``<Trade>`` execution rows share the same source
  ``ibOrderID`` and therefore the same synthetic value, preserving the
  parent-child link that the aggregation logic relies on.

* **Per-execution** attrs (``tradeID``, ``ibExecID``/``execID``,
  ``transactionID``, ``brokerageOrderID``, ``exchOrderId``, ``extExecID``)
  get a 1-indexed counter substituted into a template, so the 1st fill
  row gets ``{n}=1``, the 2nd ``{n}=2``, etc.  These counters increment
  only on **fill** elements (``<Trade>``, ``<TradeConfirm>``,
  ``<TradeConfirmation>``), not on ``<Order>`` summary rows — Order rows
  carry empty values for these fields and must not advance the counter,
  otherwise per-row uniqueness would drift across executions.

Trimming is **order-block-scoped**: ``max_orders`` distinct order IDs are
kept (in document order), along with every ``<Order>``/``<Trade>``/
``<TradeConfirm>`` element that references one of them.  This preserves
multi-fill orders intact (all executions of a kept order survive) instead
of cutting in the middle of an order's execution stream.

Idempotent: re-running on an already-sanitized file produces identical
output (order #1 always maps to ``333333331``, fill row #1 always gets
``{n}=1``, etc.).
"""

import re
import sys
from collections.abc import Callable
from itertools import count
from pathlib import Path
from re import Match

# Maximum number of distinct orders to keep.  An "order" here is a unique
# ``ibOrderID``/``orderID`` value; trimming preserves all execution rows
# for the kept orders so multi-fill aggregation paths remain testable.
_MAX_ORDERS = 6

# Element tags that represent **executions** (carry per-fill identifiers).
# Mirrors the parser's ``_FILL_TAGS``.  Notably excludes ``<Order>``, which
# is a summary row — see the per-execution counter discussion in the
# module docstring.
_FILL_ELEMENT_TAGS: tuple[str, ...] = (
    "TradeConfirmation", "TradeConfirm", "Trade",
)

# All element tags that carry an order ID and may need trimming/remapping.
# ``<Order>`` is included here even though it isn't a fill — it shares
# ``ibOrderID`` with its child ``<Trade>`` rows and must be remapped
# alongside them.
_ORDER_BEARING_TAGS: tuple[str, ...] = ("Order", *_FILL_ELEMENT_TAGS)

# Account-level — identical value across all rows.
_STATIC: dict[str, str] = {
    "accountId": "UXXXXXXX",
    "acctAlias": "",
    "model": "",
    "traderID": "",
    # Relational / origin IDs (empty on paper; redact defensively for prod).
    "relatedTradeID": "",
    "relatedTransactionID": "",
    "origTradeID": "",
    "origOrderID": "0",
    "origTransactionID": "0",
}

# Per-execution — ``{n}`` is a 1-indexed counter that advances **only on
# fill elements** (Trade / TradeConfirm / TradeConfirmation).  Order rows
# are excluded so their empty-string values don't shift the counter.
#
# AF and TC use different attribute names for the same identifiers
# (``ibExecID`` vs ``execID``); both map to the same template so an AF
# fixture and a TC fixture with the same number of rows produce
# identical synthetic execIds at equal row indices — which keeps test
# expectations interchangeable between the two report types.
_PER_EXECUTION: dict[str, str] = {
    "tradeID": "111111111{n}",
    "transactionID": "2222222222{n}",
    "brokerageOrderID": "002e.00018d9{n}.01.01",
    "exchOrderId": "002e.0001.0000{n}",
    "extExecID": "AAAAA{n}",
    "ibExecID": "00018d97.0000000{n}.01.01",
    "execID": "00018d97.0000000{n}.01.01",
}

# Order-id template — ``{n}`` is the 1-indexed position of the order in
# document-order discovery.  AF uses ``ibOrderID``; TC uses ``orderID``;
# both map through the same per-source mapping (see ``_build_order_id_map``).
_ORDER_ID_TEMPLATE = "33333333{n}"

# Compiled patterns reused across passes.
_ELEMENT_PATTERN = re.compile(
    rf'\s*<(?:{"|".join(_ORDER_BEARING_TAGS)})\b[^>]*?/>',
    re.DOTALL,
)
_FILL_ELEMENT_PATTERN = re.compile(
    rf'<(?:{"|".join(_FILL_ELEMENT_TAGS)})\b[^>]*?/>',
    re.DOTALL,
)
_ORDER_ID_ATTR_PATTERN = re.compile(r'\b(ibOrderID|orderID)="([^"]*)"')


def _extract_order_id(element_text: str) -> str:
    """Return the ``ibOrderID``/``orderID`` value from an element, or ``""``."""
    match = _ORDER_ID_ATTR_PATTERN.search(element_text)
    return match.group(2) if match else ""


def _build_order_id_map(xml_text: str, max_orders: int) -> dict[str, str]:
    """Map source order IDs → synthetic IDs in document-order, capped at *max_orders*.

    Empty order IDs are skipped — they would otherwise collapse all
    rows-without-an-orderID onto a single synthetic value, which the
    parser's dedup logic isn't designed to handle.
    """
    discovered: list[str] = []
    seen: set[str] = set()
    for match in _ELEMENT_PATTERN.finditer(xml_text):
        oid = _extract_order_id(match.group(0))
        if oid and oid not in seen:
            seen.add(oid)
            discovered.append(oid)
            if len(discovered) >= max_orders:
                break
    return {
        oid: _ORDER_ID_TEMPLATE.format(n=i + 1)
        for i, oid in enumerate(discovered)
    }


def _trim_to_kept_orders(
    xml_text: str, kept_order_ids: set[str],
) -> str:
    """Drop Order/Trade/TradeConfirm elements whose order ID isn't kept.

    Elements without an order ID at all are dropped too — they can't be
    associated with any kept order and would orphan the test data.
    """
    def maybe_drop(match: Match[str]) -> str:
        oid = _extract_order_id(match.group(0))
        return match.group(0) if oid in kept_order_ids else ""

    return _ELEMENT_PATTERN.sub(maybe_drop, xml_text)


def _apply_static(xml_text: str) -> str:
    """Replace account-level identifiers with constants — applied globally."""
    out = xml_text
    for attr, value in _STATIC.items():
        out = re.sub(
            rf'\b{re.escape(attr)}="[^"]*"',
            f'{attr}="{value}"',
            out,
        )
    return out


def _apply_order_id_map(
    xml_text: str, order_id_map: dict[str, str],
) -> str:
    """Replace ``ibOrderID``/``orderID`` values per the document-order mapping.

    Applied globally — an Order summary and all its child Trade rows share
    the same source ID and therefore receive the same synthetic ID.
    """
    def replace(match: Match[str]) -> str:
        attr_name = match.group(1)
        source_id = match.group(2)
        new_id = order_id_map.get(source_id, source_id)
        return f'{attr_name}="{new_id}"'

    return _ORDER_ID_ATTR_PATTERN.sub(replace, xml_text)


def _apply_per_execution(xml_text: str) -> str:
    """Replace per-execution IDs, scoped to fill elements only.

    Counters advance per fill element (one ``{n}`` per Trade/TradeConfirm/
    TradeConfirmation), never on ``<Order>`` rows.  Counters are shared
    across the whole document so AF row-1 and TC row-1 receive the same
    synthetic ``ibExecID``/``execID``.
    """
    counters: dict[str, Callable[[], int]] = {
        attr: count(1).__next__ for attr in _PER_EXECUTION
    }

    def sanitize_one_fill(match: Match[str]) -> str:
        text = match.group(0)
        for attr, template in _PER_EXECUTION.items():
            new_value = template.format(n=counters[attr]())
            text = re.sub(
                rf'\b{re.escape(attr)}="[^"]*"',
                f'{attr}="{new_value}"',
                text, count=1,
            )
        return text

    return _FILL_ELEMENT_PATTERN.sub(sanitize_one_fill, xml_text)


def sanitize(xml_text: str, max_orders: int = _MAX_ORDERS) -> str:
    """Return *xml_text* trimmed to ``max_orders`` distinct orders, with
    sensitive attribute values replaced.

    The pipeline is:

    1. Build the source→synthetic order ID map (document order).
    2. Drop elements whose order ID isn't in the keep set.
    3. Replace account-level static fields globally.
    4. Replace order IDs globally (Order rows + their Trades, in lockstep).
    5. Replace per-execution IDs on fill elements only.
    """
    order_id_map = _build_order_id_map(xml_text, max_orders)
    out = _trim_to_kept_orders(xml_text, set(order_id_map))
    out = _apply_static(out)
    out = _apply_order_id_map(out, order_id_map)
    out = _apply_per_execution(out)
    return out


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(f"Usage: {sys.argv[0]} INPUT.xml OUTPUT.xml")

    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    if not src.exists():
        sys.exit(f"Input file not found: {src}")

    dst.write_text(sanitize(src.read_text()))
    print(f"Wrote sanitized fixture to {dst}")


if __name__ == "__main__":
    main()
