"""Comprehensive tests for poller/flex_parser.py."""

import pytest

from models_poller import BuySell, Fill, Trade
from poller.flex_parser import _dedup_id, aggregate_fills, parse_fills

# ── Helpers ──────────────────────────────────────────────────────────────

def _wrap_af(*trade_elements: str) -> str:
    """Wrap <Trade> elements in a minimal Activity Flex XML document."""
    inner = "\n".join(trade_elements)
    return (
        "<FlexQueryResponse>"
        "<FlexStatements>"
        "<FlexStatement>"
        f"<Trades>{inner}</Trades>"
        "</FlexStatement>"
        "</FlexStatements>"
        "</FlexQueryResponse>"
    )


def _wrap_tc(*confirm_elements: str) -> str:
    """Wrap <TradeConfirm> elements in a minimal TC XML document."""
    inner = "\n".join(confirm_elements)
    return (
        "<FlexQueryResponse>"
        "<FlexStatements>"
        "<FlexStatement>"
        f"<TradeConfirms>{inner}</TradeConfirms>"
        "</FlexStatement>"
        "</FlexStatements>"
        "</FlexQueryResponse>"
    )


# ── Sample rows ──────────────────────────────────────────────────────────
# Based on real IBKR Flex XML (values sanitised).

AF_AAPL = (
    '<Trade accountId="UXXXXXXX" currency="USD" fxRateToBase="1"'
    ' assetCategory="STK" symbol="AAPL" description="APPLE INC"'
    ' conid="265598" securityID="US0378331005" securityIDType="ISIN"'
    ' cusip="037833100" isin="US0378331005" listingExchange="NASDAQ"'
    ' underlyingConid="" underlyingSymbol="" multiplier="1"'
    ' tradeID="1111111111" ibExecID="00018d97.00000001.01.01"'
    ' brokerageOrderID="002e.00018d97.01.01"'
    ' transactionID="22222222222" ibOrderID="333333333"'
    ' transactionType="ExchTrade" exchange="ISLAND"'
    ' buySell="BUY" quantity="1" tradePrice="254.6"'
    ' taxes="0" ibCommission="-0.62125" ibCommissionCurrency="USD"'
    ' cost="254.6" fifoPnlRealized="0" tradeMoney="254.6"'
    ' proceeds="-254.6" netCash="-255.22125" closePrice="254.49"'
    ' mtmPnl="-0.11" tradeDate="20250403" dateTime="20250403;153000"'
    ' reportDate="20250403" settleDateTarget="20250407"'
    ' openCloseIndicator="O" notes="P" orderTime="20250403;152959"'
    ' openDateTime="" holdingPeriodDateTime="" levelOfDetail="EXECUTION"'
    ' orderType="MKT" isAPIOrder="Y" />'
)

AF_GOOG = (
    '<Trade accountId="UXXXXXXX" currency="USD" fxRateToBase="1"'
    ' assetCategory="STK" symbol="GOOG" description="ALPHABET INC-CL C"'
    ' conid="191220310" securityID="US02079K1079" securityIDType="ISIN"'
    ' cusip="02079K107" isin="US02079K1079" listingExchange="NASDAQ"'
    ' underlyingConid="" underlyingSymbol="" multiplier="1"'
    ' tradeID="2222222222" ibExecID="00018d98.00000002.01.01"'
    ' brokerageOrderID="002e.00018d98.01.01"'
    ' transactionID="33333333333" ibOrderID="444444444"'
    ' transactionType="ExchTrade" exchange="ISLAND"'
    ' buySell="BUY" quantity="15" tradePrice="176.214"'
    ' taxes="0" ibCommission="-3.90625" ibCommissionCurrency="USD"'
    ' cost="2643.21" fifoPnlRealized="0" tradeMoney="2643.21"'
    ' proceeds="-2643.21" netCash="-2647.11625" closePrice="161.42"'
    ' mtmPnl="-221.91" tradeDate="20250403" dateTime="20250403;153001"'
    ' reportDate="20250403" settleDateTarget="20250407"'
    ' openCloseIndicator="O" notes="P" orderTime="20250403;152959"'
    ' openDateTime="" holdingPeriodDateTime="" levelOfDetail="EXECUTION"'
    ' orderType="MKT" isAPIOrder="Y" />'
)

TC_AAPL = (
    '<TradeConfirm accountId="UXXXXXXX" currency="USD"'
    ' assetCategory="STK" symbol="AAPL" description="APPLE INC"'
    ' conid="265598" securityID="US0378331005" securityIDType="ISIN"'
    ' cusip="037833100" isin="US0378331005" listingExchange="NASDAQ"'
    ' multiplier="1" tradeID="1111111111" orderID="333333333"'
    ' execID="00018d97.00000001.01.01"'
    ' brokerageOrderID="002e.00018d97.01.01"'
    ' transactionType="ExchTrade" exchange="ISLAND"'
    ' buySell="BUY" quantity="1" price="254.6"'
    ' amount="254.6" proceeds="-254.6" netCash="-255.22125"'
    ' commission="-0.62125" commissionCurrency="USD"'
    ' tax="0" settleDate="20250407"'
    ' tradeDate="20250403" dateTime="20250403;153000"'
    ' reportDate="20250403" openCloseIndicator="O"'
    ' notes="P" orderTime="20250403;152959"'
    ' orderType="MKT" isAPIOrder="Y" />'
)

TC_GOOG = (
    '<TradeConfirm accountId="UXXXXXXX" currency="USD"'
    ' assetCategory="STK" symbol="GOOG" description="ALPHABET INC-CL C"'
    ' conid="191220310" securityID="US02079K1079" securityIDType="ISIN"'
    ' cusip="02079K107" isin="US02079K1079" listingExchange="NASDAQ"'
    ' multiplier="1" tradeID="2222222222" orderID="444444444"'
    ' execID="00018d98.00000002.01.01"'
    ' brokerageOrderID="002e.00018d98.01.01"'
    ' transactionType="ExchTrade" exchange="ISLAND"'
    ' buySell="BUY" quantity="15" price="176.214"'
    ' amount="2643.21" proceeds="-2643.21" netCash="-2647.11625"'
    ' commission="-3.90625" commissionCurrency="USD"'
    ' tax="0" settleDate="20250407"'
    ' tradeDate="20250403" dateTime="20250403;153001"'
    ' reportDate="20250403" openCloseIndicator="O"'
    ' notes="P" orderTime="20250403;152959"'
    ' orderType="MKT" isAPIOrder="Y" />'
)


# ═════════════════════════════════════════════════════════════════════════
#  parse_fills() tests
# ═════════════════════════════════════════════════════════════════════════

class TestParseFillsBasic:
    """Parse well-formed AF and TC documents."""

    def test_activity_flex_basic(self) -> None:
        xml = _wrap_af(AF_AAPL, AF_GOOG)
        fills, _errors = parse_fills(xml)
        assert len(fills) == 2
        assert fills[0].symbol == "AAPL"
        assert fills[1].symbol == "GOOG"

    def test_trade_confirmation_basic(self) -> None:
        xml = _wrap_tc(TC_AAPL, TC_GOOG)
        fills, _errors = parse_fills(xml)
        assert len(fills) == 2
        assert fills[0].symbol == "AAPL"
        assert fills[1].symbol == "GOOG"

    def test_empty_trades_section(self) -> None:
        xml = _wrap_af()
        fills, errors = parse_fills(xml)
        assert fills == []
        assert errors == []

    def test_empty_document_no_crash(self) -> None:
        xml = "<FlexQueryResponse><FlexStatements></FlexStatements></FlexQueryResponse>"
        fills, errors = parse_fills(xml)
        assert fills == []
        assert errors == []

    def test_malformed_xml_returns_error(self) -> None:
        fills, errors = parse_fills("this is not xml at all <<<")
        assert fills == []
        assert len(errors) == 1
        assert "Failed to parse Flex XML" in errors[0]

    def test_missing_buySell_reports_error(self) -> None:
        """buySell is required — a fill without it should be skipped with an error."""
        xml = (
            '<FlexQueryResponse><FlexStatements><FlexStatement>'
            '<Trades>'
            '<Trade transactionID="999" symbol="AAPL" quantity="1" tradePrice="150" />'
            '</Trades>'
            '</FlexStatement></FlexStatements></FlexQueryResponse>'
        )
        fills, errors = parse_fills(xml)
        assert fills == []
        assert len(errors) == 1
        assert "Failed to create Fill" in errors[0]


# ═════════════════════════════════════════════════════════════════════════
#  Alias / field normalization
# ═════════════════════════════════════════════════════════════════════════

class TestFieldNormalization:
    """AF and TC use different XML attribute names for the same Fill field."""

    def test_af_ibCommission_becomes_commission(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].commission == pytest.approx(-0.62125)

    def test_af_ibCommissionCurrency_becomes_commissionCurrency(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].commissionCurrency == "USD"

    def test_af_ibOrderID_becomes_orderId(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].orderId == "333333333"

    def test_af_tradePrice_becomes_price(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].price == pytest.approx(254.6)

    def test_af_ibExecID_becomes_ibExecId(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].ibExecId == "00018d97.00000001.01.01"

    def test_af_transactionID_becomes_transactionId(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].transactionId == "22222222222"

    def test_tc_orderID_becomes_orderId(self) -> None:
        xml = _wrap_tc(TC_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].orderId == "333333333"

    def test_tc_execID_becomes_ibExecId(self) -> None:
        xml = _wrap_tc(TC_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].ibExecId == "00018d97.00000001.01.01"

    def test_tc_tax_becomes_taxes(self) -> None:
        xml = _wrap_tc(TC_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].taxes == pytest.approx(0.0)

    def test_tc_settleDate_becomes_settleDateTarget(self) -> None:
        xml = _wrap_tc(TC_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].settleDateTarget == "20250407"

    def test_tc_amount_becomes_tradeMoney(self) -> None:
        xml = _wrap_tc(TC_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].tradeMoney == pytest.approx(254.6)

    def test_tc_price_maps_directly(self) -> None:
        xml = _wrap_tc(TC_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].price == pytest.approx(254.6)

    def test_tc_commission_maps_directly(self) -> None:
        xml = _wrap_tc(TC_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].commission == pytest.approx(-0.62125)


# ═════════════════════════════════════════════════════════════════════════
#  AF vs TC parity — same trade, same canonical values
# ═════════════════════════════════════════════════════════════════════════

class TestAFTCParity:
    """The same trade parsed from AF and TC should yield identical canonical values."""

    @pytest.fixture()
    def af_fill(self) -> Fill:
        fills, _ = parse_fills(_wrap_af(AF_AAPL))
        return fills[0]

    @pytest.fixture()
    def tc_fill(self) -> Fill:
        fills, _ = parse_fills(_wrap_tc(TC_AAPL))
        return fills[0]

    def test_symbol_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.symbol == tc_fill.symbol == "AAPL"

    def test_orderId_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.orderId == tc_fill.orderId == "333333333"

    def test_ibExecId_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.ibExecId == tc_fill.ibExecId

    def test_price_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.price == tc_fill.price

    def test_quantity_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.quantity == tc_fill.quantity

    def test_commission_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.commission == tc_fill.commission

    def test_taxes_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.taxes == tc_fill.taxes

    def test_tradeMoney_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.tradeMoney == tc_fill.tradeMoney

    def test_proceeds_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.proceeds == tc_fill.proceeds

    def test_netCash_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.netCash == tc_fill.netCash

    def test_settleDateTarget_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.settleDateTarget == tc_fill.settleDateTarget == "20250407"

    def test_tradeDate_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.tradeDate == tc_fill.tradeDate

    def test_dateTime_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.dateTime == tc_fill.dateTime


# ═════════════════════════════════════════════════════════════════════════
#  Float parsing
# ═════════════════════════════════════════════════════════════════════════

class TestFloatParsing:
    """Float fields are parsed robustly, with errors for bad values."""

    def test_positive_float(self) -> None:
        xml = _wrap_af(
            '<Trade transactionID="1" buySell="BUY" quantity="42.5" tradePrice="100.25" />'
        )
        fills, _errors = parse_fills(xml)
        assert fills[0].quantity == pytest.approx(42.5)
        assert fills[0].price == pytest.approx(100.25)

    def test_negative_float(self) -> None:
        xml = _wrap_af(
            '<Trade transactionID="1" buySell="BUY" ibCommission="-1.5" />'
        )
        fills, _ = parse_fills(xml)
        assert fills[0].commission == pytest.approx(-1.5)

    def test_zero(self) -> None:
        xml = _wrap_af('<Trade transactionID="1" buySell="BUY" taxes="0" />')
        fills, _ = parse_fills(xml)
        assert fills[0].taxes == 0.0

    def test_empty_string_becomes_zero(self) -> None:
        xml = _wrap_af('<Trade transactionID="1" buySell="BUY" quantity="" />')
        fills, _ = parse_fills(xml)
        assert fills[0].quantity == 0.0

    def test_bad_float_reports_error(self) -> None:
        xml = _wrap_af('<Trade transactionID="1" buySell="BUY" quantity="abc" />')
        fills, errors = parse_fills(xml)
        assert fills[0].quantity == 0.0
        assert any("Bad float" in e and "quantity" in e for e in errors)

    def test_bad_float_includes_value_in_error(self) -> None:
        xml = _wrap_af('<Trade transactionID="1" buySell="BUY" tradePrice="N/A" />')
        _fills, errors = parse_fills(xml)
        assert any("N/A" in e for e in errors)

    def test_string_field_not_parsed_as_float(self) -> None:
        xml = _wrap_af(
            '<Trade transactionID="1" symbol="AAPL" buySell="BUY" />'
        )
        fills, _ = parse_fills(xml)
        assert fills[0].symbol == "AAPL"
        assert fills[0].buySell == "BUY"


# ═════════════════════════════════════════════════════════════════════════
#  Deduplication
# ═════════════════════════════════════════════════════════════════════════

class TestDedup:
    """Fills with the same dedup ID are not duplicated."""

    def test_duplicate_transactionId_deduped(self) -> None:
        xml = _wrap_af(
            '<Trade transactionID="999" buySell="BUY" symbol="AAPL" />',
            '<Trade transactionID="999" buySell="BUY" symbol="AAPL" />',
        )
        fills, _ = parse_fills(xml)
        assert len(fills) == 1

    def test_duplicate_ibExecId_deduped(self) -> None:
        xml = _wrap_af(
            '<Trade ibExecID="exec.001" buySell="BUY" symbol="X" />',
            '<Trade ibExecID="exec.001" buySell="BUY" symbol="X" />',
        )
        fills, _ = parse_fills(xml)
        assert len(fills) == 1

    def test_duplicate_tradeID_deduped(self) -> None:
        xml = _wrap_af(
            '<Trade tradeID="T1" buySell="BUY" symbol="X" />',
            '<Trade tradeID="T1" buySell="BUY" symbol="X" />',
        )
        fills, _ = parse_fills(xml)
        assert len(fills) == 1

    def test_different_ids_not_deduped(self) -> None:
        xml = _wrap_af(
            '<Trade transactionID="1" buySell="BUY" symbol="AAPL" />',
            '<Trade transactionID="2" buySell="BUY" symbol="GOOG" />',
        )
        fills, _ = parse_fills(xml)
        assert len(fills) == 2

    def test_fill_with_no_id_skipped(self) -> None:
        """A fill with no transactionId, ibExecId, or tradeID is skipped."""
        xml = _wrap_af('<Trade buySell="BUY" symbol="AAPL" />')
        fills, _ = parse_fills(xml)
        assert len(fills) == 0

    def test_cross_format_dedup(self) -> None:
        """Same trade in both <Trade> and <TradeConfirm> is deduped by ibExecId."""
        xml = (
            "<FlexQueryResponse><FlexStatements><FlexStatement>"
            "<Trades>"
            '<Trade ibExecID="exec.001" buySell="BUY" symbol="AAPL" />'
            "</Trades>"
            "<TradeConfirms>"
            '<TradeConfirm execID="exec.001" buySell="BUY" symbol="AAPL" />'
            "</TradeConfirms>"
            "</FlexStatement></FlexStatements></FlexQueryResponse>"
        )
        fills, _ = parse_fills(xml)
        # Both have dedup_id = "exec.001" (ibExecId), so only one kept
        assert len(fills) == 1


# ═════════════════════════════════════════════════════════════════════════
#  _dedup_id fallback chain
# ═════════════════════════════════════════════════════════════════════════

class TestDedupId:
    """_dedup_id returns the best available unique identifier."""

    def test_prefers_transactionId(self) -> None:
        fill = Fill(buySell=BuySell.BUY, source="flex", transactionId="T1", ibExecId="E1", tradeID="X1")
        assert _dedup_id(fill) == "T1"

    def test_falls_back_to_ibExecId(self) -> None:
        fill = Fill(buySell=BuySell.BUY, source="flex", transactionId="", ibExecId="E1", tradeID="X1")
        assert _dedup_id(fill) == "E1"

    def test_falls_back_to_tradeID(self) -> None:
        fill = Fill(buySell=BuySell.BUY, source="flex", transactionId="", ibExecId="", tradeID="X1")
        assert _dedup_id(fill) == "X1"

    def test_empty_when_no_ids(self) -> None:
        fill = Fill(buySell=BuySell.BUY, source="flex")
        assert _dedup_id(fill) == ""


# ═════════════════════════════════════════════════════════════════════════
#  Unknown attributes
# ═════════════════════════════════════════════════════════════════════════

class TestUnknownAttributes:
    """Attributes not in the model are reported but do not crash."""

    def test_unknown_attr_reported(self) -> None:
        xml = _wrap_af(
            '<Trade transactionID="1" buySell="BUY" symbol="AAPL" fakeField="xyz" />'
        )
        fills, errors = parse_fills(xml)
        assert len(fills) == 1
        assert any("fakeField" in e for e in errors)

    def test_unknown_attr_reported_once(self) -> None:
        """Same unknown field on multiple rows → reported only once."""
        xml = _wrap_af(
            '<Trade transactionID="1" buySell="BUY" fakeField="a" />',
            '<Trade transactionID="2" buySell="BUY" fakeField="b" />',
        )
        _fills, errors = parse_fills(xml)
        count = sum(1 for e in errors if "fakeField" in e)
        assert count == 1

    def test_tc_specific_fields_reported_as_unknown(self) -> None:
        """TC-only attributes (no model field) are correctly reported."""
        xml = _wrap_tc(
            '<TradeConfirm tradeID="1" buySell="BUY" blockID="99" code="P" salesTax="0" />'
        )
        _fills, errors = parse_fills(xml)
        unknown_line = next(e for e in errors if "Unknown XML" in e)
        assert "blockID" in unknown_line
        assert "code" in unknown_line
        assert "salesTax" in unknown_line

    def test_multiple_unknown_attrs_sorted(self) -> None:
        xml = _wrap_af(
            '<Trade transactionID="1" buySell="BUY" zzz="1" aaa="2" mmm="3" />'
        )
        _, errors = parse_fills(xml)
        unknown_line = next(e for e in errors if "Unknown XML" in e)
        # Attributes should be sorted alphabetically
        idx_a = unknown_line.index("aaa")
        idx_m = unknown_line.index("mmm")
        idx_z = unknown_line.index("zzz")
        assert idx_a < idx_m < idx_z


# ═════════════════════════════════════════════════════════════════════════
#  Malformed rows
# ═════════════════════════════════════════════════════════════════════════

class TestMalformedRows:
    """Broken rows are skipped and reported."""

    def test_valid_rows_still_parsed_alongside_bad(self) -> None:
        """Multiple bad floats on separate rows: each row still creates a Fill."""
        xml = _wrap_af(
            '<Trade transactionID="1" buySell="BUY" quantity="abc" />',
            '<Trade transactionID="2" buySell="BUY" quantity="10" />',
        )
        fills, _errors = parse_fills(xml)
        assert len(fills) == 2
        assert fills[0].quantity == 0.0
        assert fills[1].quantity == 10.0


# ═════════════════════════════════════════════════════════════════════════
#  Fill tag variants
# ═════════════════════════════════════════════════════════════════════════

class TestFillTags:
    """All three supported tag names are parsed."""

    def test_trade_tag(self) -> None:
        xml = _wrap_af('<Trade transactionID="1" buySell="BUY" symbol="A" />')
        fills, _ = parse_fills(xml)
        assert len(fills) == 1

    def test_trade_confirm_tag(self) -> None:
        xml = _wrap_tc('<TradeConfirm tradeID="1" buySell="BUY" symbol="B" />')
        fills, _ = parse_fills(xml)
        assert len(fills) == 1

    def test_trade_confirmation_tag(self) -> None:
        xml = (
            "<FlexQueryResponse><FlexStatements><FlexStatement>"
            "<TradeConfirmations>"
            '<TradeConfirmation tradeID="1" buySell="BUY" symbol="C" />'
            "</TradeConfirmations>"
            "</FlexStatement></FlexStatements></FlexQueryResponse>"
        )
        fills, _ = parse_fills(xml)
        assert len(fills) == 1
        assert fills[0].symbol == "C"


# ═════════════════════════════════════════════════════════════════════════
#  All fields round-trip
# ═════════════════════════════════════════════════════════════════════════

class TestAllFieldsRoundTrip:
    """A row with every canonical field set ends up on the Fill correctly."""

    def test_af_all_key_fields(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        f = fills[0]
        assert f.accountId == "UXXXXXXX"
        assert f.currency == "USD"
        assert f.fxRateToBase == pytest.approx(1.0)
        assert f.assetCategory == "STK"
        assert f.symbol == "AAPL"
        assert f.description == "APPLE INC"
        assert f.conid == "265598"
        assert f.securityID == "US0378331005"
        assert f.securityIDType == "ISIN"
        assert f.cusip == "037833100"
        assert f.isin == "US0378331005"
        assert f.listingExchange == "NASDAQ"
        assert f.multiplier == "1"
        assert f.tradeID == "1111111111"
        assert f.ibExecId == "00018d97.00000001.01.01"
        assert f.brokerageOrderID == "002e.00018d97.01.01"
        assert f.transactionId == "22222222222"
        assert f.orderId == "333333333"
        assert f.transactionType == "ExchTrade"
        assert f.exchange == "ISLAND"
        assert f.buySell == "BUY"
        assert f.quantity == pytest.approx(1.0)
        assert f.price == pytest.approx(254.6)
        assert f.taxes == pytest.approx(0.0)
        assert f.commission == pytest.approx(-0.62125)
        assert f.commissionCurrency == "USD"
        assert f.cost == pytest.approx(254.6)
        assert f.fifoPnlRealized == pytest.approx(0.0)
        assert f.tradeMoney == pytest.approx(254.6)
        assert f.proceeds == pytest.approx(-254.6)
        assert f.netCash == pytest.approx(-255.22125)
        assert f.closePrice == pytest.approx(254.49)
        assert f.mtmPnl == pytest.approx(-0.11)
        assert f.tradeDate == "20250403"
        assert f.dateTime == "20250403;153000"
        assert f.reportDate == "20250403"
        assert f.settleDateTarget == "20250407"
        assert f.openCloseIndicator == "O"
        assert f.notes == "P"
        assert f.orderTime == "20250403;152959"
        assert f.levelOfDetail == "EXECUTION"
        assert f.orderType == "MKT"
        assert f.isAPIOrder == "Y"


# ═════════════════════════════════════════════════════════════════════════
#  aggregate_fills() tests
# ═════════════════════════════════════════════════════════════════════════

class TestAggregateSingleFill:
    """A single fill produces a Trade with the same values."""

    def test_single_fill_passthrough(self) -> None:
        fills, _ = parse_fills(_wrap_af(AF_AAPL))
        trades = aggregate_fills(fills)
        assert len(trades) == 1
        t = trades[0]
        assert isinstance(t, Trade)
        assert t.symbol == "AAPL"
        assert t.price == pytest.approx(254.6)
        assert t.quantity == pytest.approx(1.0)
        assert t.commission == pytest.approx(-0.6212)  # rounded to 4 dp
        assert t.fillCount == 1
        assert len(t.execIds) == 1

    def test_single_fill_exec_id(self) -> None:
        fills, _ = parse_fills(_wrap_af(AF_AAPL))
        trades = aggregate_fills(fills)
        assert trades[0].execIds == ["22222222222"]


class TestAggregateMultipleFills:
    """Multiple fills for the same orderId are aggregated correctly."""

    @pytest.fixture()
    def two_fill_trade(self) -> Trade:
        """Two partial fills: 10 @ $100, 20 @ $110 → same orderId."""
        xml = _wrap_af(
            '<Trade transactionID="F1" ibOrderID="ORD1" buySell="BUY" symbol="TEST"'
            ' quantity="10" tradePrice="100" ibCommission="-1"'
            ' taxes="-0.5" cost="1000" tradeMoney="1000"'
            ' proceeds="-1000" netCash="-1001.5"'
            ' fifoPnlRealized="10" mtmPnl="5" accruedInt="0.1"'
            ' tradeDate="20250401" dateTime="20250401;100000" />',
            '<Trade transactionID="F2" ibOrderID="ORD1" buySell="BUY" symbol="TEST"'
            ' quantity="20" tradePrice="110" ibCommission="-2"'
            ' taxes="-1" cost="2200" tradeMoney="2200"'
            ' proceeds="-2200" netCash="-2203"'
            ' fifoPnlRealized="20" mtmPnl="15" accruedInt="0.2"'
            ' tradeDate="20250402" dateTime="20250402;140000" />',
        )
        fills, _ = parse_fills(xml)
        trades = aggregate_fills(fills)
        assert len(trades) == 1
        return trades[0]

    def test_weighted_average_price(self, two_fill_trade: Trade) -> None:
        # Weighted avg: (10*100 + 20*110) / (10+20) = 3200/30 = 106.666...
        assert two_fill_trade.price == pytest.approx(3200 / 30, rel=1e-6)

    def test_summed_quantity(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.quantity == pytest.approx(30.0)

    def test_summed_commission(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.commission == pytest.approx(-3.0)

    def test_summed_taxes(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.taxes == pytest.approx(-1.5)

    def test_summed_cost(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.cost == pytest.approx(3200.0)

    def test_summed_tradeMoney(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.tradeMoney == pytest.approx(3200.0)

    def test_summed_proceeds(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.proceeds == pytest.approx(-3200.0)

    def test_summed_netCash(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.netCash == pytest.approx(-3204.5)

    def test_summed_fifoPnlRealized(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.fifoPnlRealized == pytest.approx(30.0)

    def test_summed_mtmPnl(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.mtmPnl == pytest.approx(20.0)

    def test_summed_accruedInt(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.accruedInt == pytest.approx(0.3)

    def test_fill_count(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.fillCount == 2

    def test_exec_ids_collected(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.execIds == ["F1", "F2"]

    def test_last_fill_string_values(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.symbol == "TEST"

    def test_max_dateTime(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.dateTime == "20250402;140000"

    def test_max_tradeDate(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.tradeDate == "20250402"


class TestAggregateEdgeCases:
    """Aggregation corner cases."""

    def test_fills_without_orderId_skipped(self) -> None:
        xml = _wrap_af('<Trade transactionID="1" buySell="BUY" symbol="X" />')
        fills, _ = parse_fills(xml)
        assert fills[0].orderId == ""
        trades = aggregate_fills(fills)
        assert trades == []

    def test_multiple_orders_separate_trades(self) -> None:
        xml = _wrap_af(
            '<Trade transactionID="A" ibOrderID="ORD1" buySell="BUY" symbol="AAPL" quantity="1" tradePrice="100" />',
            '<Trade transactionID="B" ibOrderID="ORD2" buySell="BUY" symbol="GOOG" quantity="2" tradePrice="200" />',
        )
        fills, _ = parse_fills(xml)
        trades = aggregate_fills(fills)
        assert len(trades) == 2
        syms = {t.symbol for t in trades}
        assert syms == {"AAPL", "GOOG"}

    def test_zero_quantity_no_division_error(self) -> None:
        """All fills have quantity=0 — should not crash (division by zero)."""
        xml = _wrap_af(
            '<Trade transactionID="1" ibOrderID="ORD1" buySell="BUY" quantity="0" tradePrice="100" />',
        )
        fills, _ = parse_fills(xml)
        trades = aggregate_fills(fills)
        assert len(trades) == 1
        assert trades[0].price == 0.0

    def test_sell_negative_quantity(self) -> None:
        """Negative quantities (sells) aggregate correctly."""
        xml = _wrap_af(
            '<Trade transactionID="F1" ibOrderID="ORD1" buySell="SELL" quantity="-5" tradePrice="100" />',
            '<Trade transactionID="F2" ibOrderID="ORD1" buySell="SELL" quantity="-15" tradePrice="110" />',
        )
        fills, _ = parse_fills(xml)
        trades = aggregate_fills(fills)
        assert trades[0].quantity == pytest.approx(-20.0)
        # Weighted avg uses abs(quantity): (5*100 + 15*110) / (5+15) = 2150/20
        assert trades[0].price == pytest.approx(107.5)

    def test_trade_inherits_fill_fields(self) -> None:
        """Trade is a subclass of Fill — all string fields are present."""
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        trades = aggregate_fills(fills)
        t = trades[0]
        assert t.accountId == "UXXXXXXX"
        assert t.assetCategory == "STK"
        assert t.exchange == "ISLAND"
        assert t.orderType == "MKT"

    def test_rounding_precision(self) -> None:
        """Aggregated financial fields are rounded to 4 decimal places."""
        xml = _wrap_af(
            '<Trade transactionID="F1" ibOrderID="ORD1" buySell="BUY" quantity="3"'
            ' tradePrice="10" ibCommission="-0.333333" />',
            '<Trade transactionID="F2" ibOrderID="ORD1" buySell="BUY" quantity="3"'
            ' tradePrice="10" ibCommission="-0.333333" />',
            '<Trade transactionID="F3" ibOrderID="ORD1" buySell="BUY" quantity="3"'
            ' tradePrice="10" ibCommission="-0.333334" />',
        )
        fills, _ = parse_fills(xml)
        trades = aggregate_fills(fills)
        # Sum = -1.0 exactly, but ensure rounding to 4 dp
        assert trades[0].commission == pytest.approx(-1.0)


# ═════════════════════════════════════════════════════════════════════════
#  Full pipeline: parse_fills → aggregate_fills
# ═════════════════════════════════════════════════════════════════════════

class TestFullPipeline:
    """End-to-end: AF XML → fills → trades."""

    def test_af_two_symbols(self) -> None:
        xml = _wrap_af(AF_AAPL, AF_GOOG)
        fills, _errors = parse_fills(xml)
        trades = aggregate_fills(fills)
        assert len(trades) == 2
        for t in trades:
            assert isinstance(t, Trade)
            assert t.fillCount == 1

    def test_tc_two_symbols(self) -> None:
        xml = _wrap_tc(TC_AAPL, TC_GOOG)
        fills, _errors = parse_fills(xml)
        trades = aggregate_fills(fills)
        assert len(trades) == 2

    def test_errors_propagate(self) -> None:
        xml = _wrap_af(
            '<Trade transactionID="1" ibOrderID="O" buySell="BUY" quantity="bad" unknownField="x" />'
        )
        fills, errors = parse_fills(xml)
        assert any("Bad float" in e for e in errors)
        assert any("unknownField" in e for e in errors)
        # Fill still created with quantity=0.0
        trades = aggregate_fills(fills)
        assert len(trades) == 1
        assert trades[0].quantity == 0.0
