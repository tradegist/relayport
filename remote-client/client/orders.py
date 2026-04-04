"""Orders namespace — place, cancel, and query orders."""

import asyncio
import logging

from ib_async import IB, Contract, LimitOrder, MarketOrder, Order

from models_remote_client import ContractRequest, OrderRequest, OrderResponse

log = logging.getLogger("ib-client")


class OrdersNamespace:
    """Order operations against IB Gateway."""

    def __init__(self, ib: IB) -> None:
        self._ib = ib

    async def place(
        self,
        contract_req: ContractRequest,
        order_req: OrderRequest,
    ) -> OrderResponse:
        """Place an order and return the result.

        Raises ValueError for invalid input, RuntimeError for IB errors.
        """
        ib_order: Order
        if order_req.orderType == "LMT":
            if order_req.lmtPrice is None:
                raise ValueError("lmtPrice required for LMT orders")
            ib_order = LimitOrder(
                order_req.action,
                order_req.totalQuantity,
                order_req.lmtPrice,
            )
        else:
            ib_order = MarketOrder(order_req.action, order_req.totalQuantity)

        ib_order.tif = order_req.tif
        ib_order.outsideRth = order_req.outsideRth

        ib_contract = Contract(
            symbol=contract_req.symbol,
            secType=contract_req.secType,
            exchange=contract_req.exchange,
            currency=contract_req.currency,
            primaryExchange=contract_req.primaryExchange,
        )

        try:
            qualified = await self._ib.qualifyContractsAsync(ib_contract)
            if not qualified:
                raise ValueError(
                    f"Could not qualify contract for {contract_req.symbol}"
                )
        except ValueError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Contract qualification failed: {exc}") from exc

        log.info(
            "Placing order: %s %.4g %s %s%s",
            order_req.action,
            order_req.totalQuantity,
            contract_req.symbol,
            order_req.orderType,
            f" @ {order_req.lmtPrice}" if order_req.orderType == "LMT" else "",
        )

        try:
            trade = self._ib.placeOrder(ib_contract, ib_order)
        except Exception as exc:
            raise RuntimeError(f"Order placement failed: {exc}") from exc

        # Give IBKR a moment to acknowledge
        await asyncio.sleep(1)

        return OrderResponse(
            status=trade.orderStatus.status,
            orderId=trade.order.orderId,
            action=order_req.action,
            symbol=contract_req.symbol,
            totalQuantity=order_req.totalQuantity,
            orderType=order_req.orderType,
            lmtPrice=order_req.lmtPrice if order_req.orderType == "LMT" else None,
        )
