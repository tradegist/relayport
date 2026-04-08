"""Comprehensive tests for poller/flex_parser.py."""

import pytest

from models_poller import BuySell, Fill, Trade
from poller.flex_parser import parse_fills
from shared import aggregate_fills

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
#  Alias / field normalization — CommonFill fields
# ═════════════════════════════════════════════════════════════════════════

class TestFieldNormalization:
    """AF and TC aliases map correctly into CommonFill fields and raw dict."""

    def test_af_ibCommission_maps_to_fee(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].fee == pytest.approx(-0.62125)

    def test_af_ibCommission_preserved_in_raw(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].raw["commission"] == pytest.approx(-0.62125)

    def test_af_ibCommissionCurrency_in_raw(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].raw["commissionCurrency"] == "USD"

    def test_af_ibOrderID_maps_to_orderId(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].orderId == "333333333"

    def test_af_tradePrice_maps_to_price(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].price == pytest.approx(254.6)

    def test_af_ibExecID_maps_to_execId(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].execId == "00018d97.00000001.01.01"

    def test_af_transactionID_in_raw(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].raw["transactionId"] == "22222222222"

    def test_tc_orderID_maps_to_orderId(self) -> None:
        xml = _wrap_tc(TC_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].orderId == "333333333"

    def test_tc_execID_maps_to_execId(self) -> None:
        xml = _wrap_tc(TC_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].execId == "00018d97.00000001.01.01"

    def test_tc_tax_in_raw_as_taxes(self) -> None:
        xml = _wrap_tc(TC_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].raw["taxes"] == pytest.approx(0.0)

    def test_tc_settleDate_in_raw_as_settleDateTarget(self) -> None:
        xml = _wrap_tc(TC_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].raw["settleDateTarget"] == "20250407"

    def test_tc_amount_in_raw_as_tradeMoney(self) -> None:
        xml = _wrap_tc(TC_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].raw["tradeMoney"] == pytest.approx(254.6)

    def test_tc_price_maps_directly(self) -> None:
        xml = _wrap_tc(TC_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].price == pytest.approx(254.6)

    def test_tc_commission_maps_to_fee(self) -> None:
        xml = _wrap_tc(TC_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].fee == pytest.approx(-0.62125)

    def test_af_buySell_maps_to_side(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].side == BuySell.BUY

    def test_af_quantity_maps_to_volume(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].volume == pytest.approx(1.0)

    def test_af_dateTime_maps_to_timestamp(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].timestamp == "20250403;153000"

    def test_af_orderType_normalized(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].orderType == "market"


# ═════════════════════════════════════════════════════════════════════════
#  AF vs TC parity — same trade, same canonical values
# ═════════════════════════════════════════════════════════════════════════

class TestAFTCParity:
    """The same trade parsed from AF and TC should yield identical CommonFill values."""

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

    def test_execId_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.execId == tc_fill.execId

    def test_price_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.price == tc_fill.price

    def test_volume_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.volume == tc_fill.volume

    def test_fee_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.fee == tc_fill.fee

    def test_side_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.side == tc_fill.side == BuySell.BUY

    def test_timestamp_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.timestamp == tc_fill.timestamp

    def test_raw_taxes_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.raw["taxes"] == tc_fill.raw["taxes"]

    def test_raw_tradeMoney_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.raw["tradeMoney"] == tc_fill.raw["tradeMoney"]

    def test_raw_proceeds_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.raw["proceeds"] == tc_fill.raw["proceeds"]

    def test_raw_netCash_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.raw["netCash"] == tc_fill.raw["netCash"]

    def test_raw_settleDateTarget_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.raw["settleDateTarget"] == tc_fill.raw["settleDateTarget"] == "20250407"

    def test_raw_tradeDate_matches(self, af_fill: Fill, tc_fill: Fill) -> None:
        assert af_fill.raw["tradeDate"] == tc_fill.raw["tradeDate"]


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
        assert fills[0].volume == pytest.approx(42.5)
        assert fills[0].price == pytest.approx(100.25)

    def test_negative_float(self) -> None:
        xml = _wrap_af(
            '<Trade transactionID="1" buySell="BUY" ibCommission="-1.5" />'
        )
        fills, _ = parse_fills(xml)
        assert fills[0].fee == pytest.approx(-1.5)

    def test_zero(self) -> None:
        xml = _wrap_af('<Trade transactionID="1" buySell="BUY" taxes="0" />')
        fills, _ = parse_fills(xml)
        assert fills[0].raw["taxes"] == 0.0

    def test_empty_string_becomes_zero(self) -> None:
        xml = _wrap_af('<Trade transactionID="1" buySell="BUY" quantity="" />')
        fills, _ = parse_fills(xml)
        assert fills[0].volume == 0.0

    def test_bad_float_reports_error(self) -> None:
        xml = _wrap_af('<Trade transactionID="1" buySell="BUY" quantity="abc" />')
        fills, errors = parse_fills(xml)
        assert fills[0].volume == 0.0
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
        assert fills[0].side == BuySell.BUY


# ═════════════════════════════════════════════════════════════════════════
#  Deduplication (within a single XML document)
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
        """A fill with no transactionId, ibExecId, or tradeID is skipped with an error."""
        xml = _wrap_af('<Trade buySell="BUY" symbol="AAPL" />')
        fills, errors = parse_fills(xml)
        assert len(fills) == 0
        assert any("no execId" in e for e in errors)

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
        assert len(fills) == 1


# ═════════════════════════════════════════════════════════════════════════
#  execId fallback chain (resolved at parse time)
# ═════════════════════════════════════════════════════════════════════════

class TestExecIdFallback:
    """Parser resolves execId from ibExecId → transactionId → tradeID."""

    def test_prefers_ibExecId(self) -> None:
        xml = _wrap_af(
            '<Trade ibExecID="E1" transactionID="T1" tradeID="X1" buySell="BUY" />'
        )
        fills, _ = parse_fills(xml)
        assert fills[0].execId == "E1"

    def test_falls_back_to_transactionId(self) -> None:
        xml = _wrap_af(
            '<Trade transactionID="T1" tradeID="X1" buySell="BUY" />'
        )
        fills, _ = parse_fills(xml)
        assert fills[0].execId == "T1"

    def test_falls_back_to_tradeID(self) -> None:
        xml = _wrap_af(
            '<Trade tradeID="X1" buySell="BUY" />'
        )
        fills, _ = parse_fills(xml)
        assert fills[0].execId == "X1"


# ═════════════════════════════════════════════════════════════════════════
#  Raw dict — all XML attributes preserved
# ═════════════════════════════════════════════════════════════════════════

class TestRawDict:
    """All XML attributes are preserved in the raw dict."""

    def test_extra_attrs_in_raw(self) -> None:
        xml = _wrap_af(
            '<Trade transactionID="1" buySell="BUY" symbol="AAPL" fakeField="xyz" />'
        )
        fills, _ = parse_fills(xml)
        assert fills[0].raw["fakeField"] == "xyz"

    def test_raw_has_canonicalized_names(self) -> None:
        """Aliases apply in raw: ibCommission → commission, ibOrderID → orderId."""
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        raw = fills[0].raw
        assert "commission" in raw  # ibCommission aliased
        assert "ibCommission" not in raw
        assert "orderId" in raw  # ibOrderID aliased
        assert "ibOrderID" not in raw

    def test_raw_floats_parsed(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert isinstance(fills[0].raw["quantity"], float)
        assert isinstance(fills[0].raw["price"], float)
        assert isinstance(fills[0].raw["commission"], float)

    def test_raw_strings_preserved(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        assert fills[0].raw["symbol"] == "AAPL"
        assert fills[0].raw["buySell"] == "BUY"  # Original XML value, not enum


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
        assert fills[0].volume == 0.0
        assert fills[1].volume == 10.0


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
#  All fields round-trip: CommonFill fields + raw dict
# ═════════════════════════════════════════════════════════════════════════

class TestAllFieldsRoundTrip:
    """A row with every field set produces correct CommonFill + raw dict."""

    def test_af_common_fill_fields(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        f = fills[0]
        assert f.execId == "00018d97.00000001.01.01"
        assert f.orderId == "333333333"
        assert f.symbol == "AAPL"
        assert f.side == BuySell.BUY
        assert f.orderType == "market"
        assert f.price == pytest.approx(254.6)
        assert f.volume == pytest.approx(1.0)
        assert f.cost == pytest.approx(254.6)
        assert f.fee == pytest.approx(-0.62125)
        assert f.timestamp == "20250403;153000"
        assert f.source == "flex"

    def test_af_raw_dict_ibkr_fields(self) -> None:
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        raw = fills[0].raw
        assert raw["accountId"] == "UXXXXXXX"
        assert raw["currency"] == "USD"
        assert raw["fxRateToBase"] == pytest.approx(1.0)
        assert raw["assetCategory"] == "STK"
        assert raw["symbol"] == "AAPL"
        assert raw["description"] == "APPLE INC"
        assert raw["conid"] == "265598"
        assert raw["securityID"] == "US0378331005"
        assert raw["securityIDType"] == "ISIN"
        assert raw["cusip"] == "037833100"
        assert raw["isin"] == "US0378331005"
        assert raw["listingExchange"] == "NASDAQ"
        assert raw["multiplier"] == "1"
        assert raw["tradeID"] == "1111111111"
        assert raw["ibExecId"] == "00018d97.00000001.01.01"
        assert raw["brokerageOrderID"] == "002e.00018d97.01.01"
        assert raw["transactionId"] == "22222222222"
        assert raw["orderId"] == "333333333"
        assert raw["transactionType"] == "ExchTrade"
        assert raw["exchange"] == "ISLAND"
        assert raw["buySell"] == "BUY"
        assert raw["quantity"] == pytest.approx(1.0)
        assert raw["price"] == pytest.approx(254.6)
        assert raw["taxes"] == pytest.approx(0.0)
        assert raw["commission"] == pytest.approx(-0.62125)
        assert raw["commissionCurrency"] == "USD"
        assert raw["cost"] == pytest.approx(254.6)
        assert raw["fifoPnlRealized"] == pytest.approx(0.0)
        assert raw["tradeMoney"] == pytest.approx(254.6)
        assert raw["proceeds"] == pytest.approx(-254.6)
        assert raw["netCash"] == pytest.approx(-255.22125)
        assert raw["closePrice"] == pytest.approx(254.49)
        assert raw["mtmPnl"] == pytest.approx(-0.11)
        assert raw["tradeDate"] == "20250403"
        assert raw["dateTime"] == "20250403;153000"
        assert raw["reportDate"] == "20250403"
        assert raw["settleDateTarget"] == "20250407"
        assert raw["openCloseIndicator"] == "O"
        assert raw["notes"] == "P"
        assert raw["orderTime"] == "20250403;152959"
        assert raw["levelOfDetail"] == "EXECUTION"
        assert raw["orderType"] == "MKT"
        assert raw["isAPIOrder"] == "Y"


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
        assert t.volume == pytest.approx(1.0)
        assert t.fee == pytest.approx(-0.6212)  # rounded to 4 dp
        assert t.fillCount == 1
        assert len(t.execIds) == 1

    def test_single_fill_exec_id(self) -> None:
        fills, _ = parse_fills(_wrap_af(AF_AAPL))
        trades = aggregate_fills(fills)
        assert trades[0].execIds == ["00018d97.00000001.01.01"]


class TestAggregateMultipleFills:
    """Multiple fills for the same orderId are aggregated correctly."""

    @pytest.fixture()
    def two_fill_trade(self) -> Trade:
        """Two partial fills: 10 @ $100, 20 @ $110 → same orderId."""
        xml = _wrap_af(
            '<Trade transactionID="F1" ibOrderID="ORD1" buySell="BUY" symbol="TEST"'
            ' quantity="10" tradePrice="100" ibCommission="-1"'
            ' cost="1000" dateTime="20250401;100000" />',
            '<Trade transactionID="F2" ibOrderID="ORD1" buySell="BUY" symbol="TEST"'
            ' quantity="20" tradePrice="110" ibCommission="-2"'
            ' cost="2200" dateTime="20250402;140000" />',
        )
        fills, _ = parse_fills(xml)
        trades = aggregate_fills(fills)
        assert len(trades) == 1
        return trades[0]

    def test_weighted_average_price(self, two_fill_trade: Trade) -> None:
        # Weighted avg: (10*100 + 20*110) / (10+20) = 3200/30 = 106.666...
        assert two_fill_trade.price == pytest.approx(3200 / 30, rel=1e-6)

    def test_summed_volume(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.volume == pytest.approx(30.0)

    def test_summed_fee(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.fee == pytest.approx(-3.0)

    def test_summed_cost(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.cost == pytest.approx(3200.0)

    def test_fill_count(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.fillCount == 2

    def test_exec_ids_collected(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.execIds == ["F1", "F2"]

    def test_last_fill_string_values(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.symbol == "TEST"

    def test_max_timestamp(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.timestamp == "20250402;140000"

    def test_raw_from_first_fill(self, two_fill_trade: Trade) -> None:
        assert two_fill_trade.raw["transactionId"] == "F1"


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
        assert trades[0].volume == pytest.approx(-20.0)
        # Weighted avg uses abs(quantity): (5*100 + 15*110) / (5+15) = 2150/20
        assert trades[0].price == pytest.approx(107.5)

    def test_trade_raw_from_first_fill(self) -> None:
        """Trade.raw is the first fill's raw dict (IBKR-specific data preserved)."""
        xml = _wrap_af(AF_AAPL)
        fills, _ = parse_fills(xml)
        trades = aggregate_fills(fills)
        t = trades[0]
        assert t.raw["accountId"] == "UXXXXXXX"
        assert t.raw["assetCategory"] == "STK"
        assert t.raw["exchange"] == "ISLAND"
        assert t.raw["orderType"] == "MKT"

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
        assert trades[0].fee == pytest.approx(-1.0)


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
            '<Trade transactionID="1" ibOrderID="O" buySell="BUY" quantity="bad" />'
        )
        fills, errors = parse_fills(xml)
        assert any("Bad float" in e for e in errors)
        # Fill still created with volume=0.0
        trades = aggregate_fills(fills)
        assert len(trades) == 1
        assert trades[0].volume == 0.0
