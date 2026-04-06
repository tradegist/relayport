"""Unit tests for client/orders.py — OrdersNamespace.place()."""

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from client.orders import OrdersNamespace
from models_remote_client import ContractPayload, OrderPayload


def _make_namespace(
    qualify_result: list[object] | Exception | None = None,
    place_result: object | Exception | None = None,
) -> OrdersNamespace:
    """Build an OrdersNamespace with a mocked IB instance."""
    ib = MagicMock()

    if isinstance(qualify_result, Exception):
        ib.qualifyContractsAsync = AsyncMock(side_effect=qualify_result)
    else:
        ib.qualifyContractsAsync = AsyncMock(
            return_value=qualify_result if qualify_result is not None else [MagicMock()]
        )

    if isinstance(place_result, Exception):
        ib.placeOrder = MagicMock(side_effect=place_result)
    else:
        trade = place_result or _mock_trade()
        ib.placeOrder = MagicMock(return_value=trade)

    return OrdersNamespace(ib)


def _mock_trade(
    order_id: int = 1, perm_id: int = 100, status: str = "Submitted",
) -> MagicMock:
    trade = MagicMock()
    trade.order.orderId = order_id
    trade.order.permId = perm_id
    trade.orderStatus.status = status
    return trade


# ── LMT price validation ────────────────────────────────────────────


class TestLmtPriceValidation:
    def test_lmt_without_price_raises(self) -> None:
        ns = _make_namespace()
        contract = ContractPayload(symbol="AAPL")
        order = OrderPayload(action="BUY", totalQuantity=1, orderType="LMT")

        with pytest.raises(ValueError, match="lmtPrice required"):
            asyncio.run(ns.place(contract, order))

    def test_lmt_with_price_succeeds(self) -> None:
        ns = _make_namespace()
        contract = ContractPayload(symbol="AAPL")
        order = OrderPayload(
            action="BUY", totalQuantity=1, orderType="LMT", lmtPrice=150.0,
        )

        result = asyncio.run(ns.place(contract, order))
        assert result.orderType == "LMT"
        assert result.lmtPrice == 150.0

    def test_mkt_ignores_lmt_price(self) -> None:
        ns = _make_namespace()
        contract = ContractPayload(symbol="AAPL")
        order = OrderPayload(action="BUY", totalQuantity=1, orderType="MKT")

        result = asyncio.run(ns.place(contract, order))
        assert result.lmtPrice is None


# ── Contract qualification ───────────────────────────────────────────


class TestContractQualification:
    def test_empty_qualification_raises_value_error(self) -> None:
        ns = _make_namespace(qualify_result=[])
        contract = ContractPayload(symbol="ZZZZZZ")
        order = OrderPayload(action="BUY", totalQuantity=1, orderType="MKT")

        with pytest.raises(ValueError, match="Could not qualify"):
            asyncio.run(ns.place(contract, order))

    def test_qualification_network_error_raises_runtime_error(self) -> None:
        ns = _make_namespace(qualify_result=ConnectionError("timeout"))
        contract = ContractPayload(symbol="AAPL")
        order = OrderPayload(action="BUY", totalQuantity=1, orderType="MKT")

        with pytest.raises(RuntimeError, match="Contract qualification failed"):
            asyncio.run(ns.place(contract, order))


# ── Order placement failures ────────────────────────────────────────


class TestPlacementFailure:
    def test_placement_error_raises_runtime_error(self) -> None:
        ns = _make_namespace(place_result=Exception("IB internal error"))
        contract = ContractPayload(symbol="AAPL")
        order = OrderPayload(action="BUY", totalQuantity=1, orderType="MKT")

        with pytest.raises(RuntimeError, match="Order placement failed"):
            asyncio.run(ns.place(contract, order))


# ── Successful response ─────────────────────────────────────────────


class TestSuccessResponse:
    def test_response_fields(self) -> None:
        ns = _make_namespace(place_result=_mock_trade(order_id=42, perm_id=999))
        contract = ContractPayload(symbol="TSLA", currency="EUR", exchange="LSE")
        order = OrderPayload(action="SELL", totalQuantity=5, orderType="MKT")

        result = asyncio.run(ns.place(contract, order))
        assert result.orderId == 999
        assert result.action == "SELL"
        assert result.symbol == "TSLA"
        assert result.totalQuantity == 5
        assert result.orderType == "MKT"

    def test_contract_defaults_applied(self) -> None:
        ns = _make_namespace()
        contract = ContractPayload(symbol="AAPL")
        order = OrderPayload(action="BUY", totalQuantity=1, orderType="MKT")

        asyncio.run(ns.place(contract, order))
        mock_ib = cast(MagicMock, ns._ib)
        call_args = mock_ib.qualifyContractsAsync.call_args
        ib_contract = call_args[0][0]
        assert ib_contract.secType == "STK"
        assert ib_contract.exchange == "SMART"
        assert ib_contract.currency == "USD"
