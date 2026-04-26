"""Tests for the Flex XML sanitizer.

These cover the order-aware behaviours that broke when ``<Order>`` summary
rows were introduced into the fixture: per-execution counters must skip
Order rows, ``ibOrderID`` must be remapped consistently across an Order
and its child Trades, and trimming must keep multi-fill orders intact.
"""

import re
import unittest

from relays.ibkr.fixtures.sanitize import (
    _MAX_ORDERS,
    _build_order_id_map,
    sanitize,
)


def _wrap(*elements: str) -> str:
    """Wrap raw <Order>/<Trade> elements in a minimal Activity Flex shell."""
    body = "\n".join(elements)
    return (
        '<FlexQueryResponse type="AF"><FlexStatements><FlexStatement>'
        f"<Trades>{body}</Trades>"
        "</FlexStatement></FlexStatements></FlexQueryResponse>"
    )


def _order(ibid: str, *, trade_id: str = "", ibexec: str = "", txn: str = "") -> str:
    """Build an <Order> summary row.  Defaults match real Flex output:
    Order rows have empty tradeID/ibExecID/transactionID."""
    return (
        f'<Order ibOrderID="{ibid}" tradeID="{trade_id}" ibExecID="{ibexec}" '
        f'transactionID="{txn}" buySell="BUY" assetCategory="STK" symbol="X" '
        f'levelOfDetail="ORDER" />'
    )


def _trade(ibid: str, *, trade_id: str, ibexec: str, txn: str) -> str:
    """Build a <Trade> execution row with realistic per-fill IDs."""
    return (
        f'<Trade ibOrderID="{ibid}" tradeID="{trade_id}" ibExecID="{ibexec}" '
        f'transactionID="{txn}" buySell="BUY" assetCategory="STK" symbol="X" '
        f'quantity="1" tradePrice="100" levelOfDetail="EXECUTION" />'
    )


def _attrs(text: str, attr: str) -> list[str]:
    """Return all values of ``attr`` in document order."""
    return re.findall(rf'\b{attr}="([^"]*)"', text)


class TestOrderIdMap(unittest.TestCase):
    """``_build_order_id_map`` discovers unique order IDs in document order."""

    def test_skips_empty_order_ids(self) -> None:
        # Trades without an ibOrderID would otherwise collapse onto a single
        # synthetic ID and trigger spurious dedup collisions in tests.
        xml = _wrap(
            _trade("", trade_id="t1", ibexec="e1", txn="x1"),
            _trade("A", trade_id="t2", ibexec="e2", txn="x2"),
        )
        result = _build_order_id_map(xml, max_orders=10)
        self.assertEqual(set(result), {"A"})

    def test_assigns_synthetic_ids_in_document_order(self) -> None:
        xml = _wrap(
            _order("ORDER_B"),
            _trade("ORDER_B", trade_id="1", ibexec="1", txn="1"),
            _order("ORDER_A"),
            _trade("ORDER_A", trade_id="2", ibexec="2", txn="2"),
        )
        result = _build_order_id_map(xml, max_orders=10)
        # First seen wins position 1.
        self.assertEqual(result["ORDER_B"], "333333331")
        self.assertEqual(result["ORDER_A"], "333333332")

    def test_caps_at_max_orders(self) -> None:
        xml = _wrap(
            *(
                _trade(f"ORD{i}", trade_id=str(i), ibexec=str(i), txn=str(i))
                for i in range(10)
            ),
        )
        result = _build_order_id_map(xml, max_orders=3)
        self.assertEqual(len(result), 3)
        # First three discovered are the ones kept.
        self.assertEqual(set(result), {"ORD0", "ORD1", "ORD2"})


class TestPerExecutionCountersIgnoreOrderRows(unittest.TestCase):
    """The per-fill counter (n) must advance only on Trade elements.

    If an interleaved ``<Order>`` row consumed a counter slot, every
    subsequent Trade would shift by one — breaking the AF/TC parity claim
    in the docstring and silently re-numbering execIds across refreshes.
    """

    def test_order_rows_do_not_shift_trade_counters(self) -> None:
        xml = _wrap(
            _order("A"),
            _trade("A", trade_id="raw1", ibexec="raw1", txn="raw1"),
            _order("B"),
            _trade("B", trade_id="raw2", ibexec="raw2", txn="raw2"),
        )
        out = sanitize(xml, max_orders=5)

        # Two Trade rows → counter values 1 and 2 (not 2 and 4).
        self.assertEqual(_attrs(out, "tradeID"), ["", "1111111111", "", "1111111112"])
        self.assertEqual(_attrs(out, "transactionID"),
                         ["", "22222222221", "", "22222222222"])
        self.assertEqual(_attrs(out, "ibExecID"),
                         ["", "00018d97.00000001.01.01",
                          "", "00018d97.00000002.01.01"])

    def test_order_row_per_fill_attrs_left_empty(self) -> None:
        # Order rows arrive with empty tradeID/ibExecID/transactionID; the
        # sanitizer must NOT inject synthetic values into them.
        xml = _wrap(_order("A"), _trade("A", trade_id="x", ibexec="x", txn="x"))
        out = sanitize(xml, max_orders=5)
        order_attrs = re.search(r"<Order [^/]*/>", out)
        assert order_attrs is not None
        order_text = order_attrs.group(0)
        self.assertIn('tradeID=""', order_text)
        self.assertIn('ibExecID=""', order_text)
        self.assertIn('transactionID=""', order_text)


class TestOrderIdSharedAcrossOrderAndTrade(unittest.TestCase):
    """An ``<Order>`` and its child ``<Trade>`` rows share one ibOrderID.

    The parser uses ``orderId`` to group fills into trades, so this link
    must survive sanitization. If the Order row got synthetic ID 1 and
    its Trade got synthetic ID 2, aggregation would split them apart.
    """

    def test_order_and_trades_share_synthetic_ibOrderID(self) -> None:
        xml = _wrap(
            _order("A"),
            _trade("A", trade_id="t1", ibexec="e1", txn="x1"),
            _trade("A", trade_id="t2", ibexec="e2", txn="x2"),
            _order("B"),
            _trade("B", trade_id="t3", ibexec="e3", txn="x3"),
        )
        out = sanitize(xml, max_orders=5)
        ib_order_ids = _attrs(out, "ibOrderID")
        # Order_A + 2 Trades → all "333333331"; Order_B + 1 Trade → all "333333332".
        self.assertEqual(ib_order_ids,
                         ["333333331", "333333331", "333333331",
                          "333333332", "333333332"])


class TestTrimmingByOrderBlocks(unittest.TestCase):
    """``max_orders`` trims by *distinct order*, keeping every execution
    of a kept order (so multi-fill aggregation stays exercised)."""

    def test_drops_excess_order_blocks_entirely(self) -> None:
        xml = _wrap(
            _order("A"),
            _trade("A", trade_id="t1", ibexec="e1", txn="x1"),
            _order("B"),
            _trade("B", trade_id="t2", ibexec="e2", txn="x2"),
            _order("C"),
            _trade("C", trade_id="t3", ibexec="e3", txn="x3"),
        )
        out = sanitize(xml, max_orders=2)
        # Two orders kept (the third dropped along with its Trade).
        self.assertEqual(out.count("<Order "), 2)
        self.assertEqual(out.count("<Trade "), 2)

    def test_keeps_all_executions_of_kept_order(self) -> None:
        xml = _wrap(
            _order("A"),
            _trade("A", trade_id="t1", ibexec="e1", txn="x1"),
            _trade("A", trade_id="t2", ibexec="e2", txn="x2"),
            _trade("A", trade_id="t3", ibexec="e3", txn="x3"),
            _order("B"),
            _trade("B", trade_id="t4", ibexec="e4", txn="x4"),
        )
        out = sanitize(xml, max_orders=1)
        # Order A kept with all 3 executions; Order B dropped.
        self.assertEqual(out.count("<Order "), 1)
        self.assertEqual(out.count("<Trade "), 3)

    def test_default_max_orders_constant(self) -> None:
        # Sanity-check the module-level default — bumping this is the
        # documented way to enlarge the committed fixture.
        self.assertGreaterEqual(_MAX_ORDERS, 4)


class TestIdempotence(unittest.TestCase):
    """Re-running ``sanitize`` on its own output must produce identical bytes.

    Both passes start from `_build_order_id_map` discovery, which is
    deterministic (document order), so the synthetic IDs are stable.
    """

    def test_double_sanitize_identical(self) -> None:
        xml = _wrap(
            _order("A"),
            _trade("A", trade_id="t1", ibexec="e1", txn="x1"),
            _trade("A", trade_id="t2", ibexec="e2", txn="x2"),
            _order("B"),
            _trade("B", trade_id="t3", ibexec="e3", txn="x3"),
        )
        once = sanitize(xml, max_orders=5)
        twice = sanitize(once, max_orders=5)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()
