"""Strict settled-cash checks and reservations owned by pending orders."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime
from typing import TYPE_CHECKING

from ..config import DataFrequency, ShareType
from ..core.shared import CASH_TOLERANCE
from ..core.state import MarketState, OrderState
from ..models import calculate_commission, calculate_slippage
from ..types import Order, OrderSide, OrderStatus, OrderType
from .settlement import us_equity_settlement_date

if TYPE_CHECKING:
    from ..broker import Broker
    from .account import AccountState


class CashAccountRules:
    """Single-currency US stock cash account; initial cash is already settled."""

    def __init__(
        self,
        broker: Broker,
        *,
        account: AccountState,
        market: MarketState,
        orders: OrderState,
        extra_holidays: tuple[str, ...],
        data_frequency: DataFrequency,
        timezone: str,
        timestamp_semantics: str | None,
    ):
        self.broker = broker
        self.account = account
        self.market = market
        self.orders = orders
        self.data_frequency = data_frequency
        self.timezone = timezone
        self.timestamp_semantics = timestamp_semantics
        self.extra_holidays = frozenset(date.fromisoformat(value) for value in extra_holidays)

    def session_date(self, timestamp: datetime) -> date:
        from ..sessions import session_date_for_timestamp

        return session_date_for_timestamp(
            timestamp,
            calendar="NYSE",
            timezone=self.timezone,
            session_start_time=None,
            data_frequency=self.data_frequency,
            timestamp_semantics=self.timestamp_semantics,
        )

    def reserved_cash(self, exclude_order_id: str = "") -> float:
        return sum(
            order._reserved_cash
            for order in self.orders.pending
            if order.status is OrderStatus.PENDING and order.order_id != exclude_order_id
        )

    def validate_fill(
        self, order: Order, quantity: float, price: float, commission: float
    ) -> tuple[bool, str]:
        """Validate actual costs before any fill, position or cash mutation."""
        broker = self.broker
        if not math.isfinite(quantity) or quantity <= 0 or not math.isfinite(commission):
            return False, "Invalid cash-account quantity or commission"
        if commission < 0:
            return False, "Negative commission not allowed in US cash account"
        candidate = replace(order, quantity=quantity)
        valid, reason = broker.gatekeeper.validate_order(candidate, price)
        if not valid:
            return valid, reason
        available = broker.gatekeeper._available_cash(order.order_id)
        cost = quantity * price + commission
        if order.side is OrderSide.SELL:
            cost = commission - quantity * price
        if cost > available + CASH_TOLERANCE:
            return False, f"Insufficient settled cash: need ${cost:,.2f}, have ${available:,.2f}"
        return True, ""

    def reserve(self, order: Order, quantity: float | None = None) -> bool:
        """Reserve estimated buy cost, including slippage/fees, until terminal state."""
        broker = self.broker
        price = self.market.prices.get(order.asset)
        if order.order_type is OrderType.LIMIT:
            price = order.limit_price
        elif order.stop_price is not None and price is not None:
            price = max(price, order.stop_price)
        if price is None or not math.isfinite(price) or price <= 0:
            order.reject("No price available for cash reservation", "price_unavailable")
            return False
        if order.quantity <= 0 or not math.isfinite(order.quantity):
            order.reject("Invalid cash-account quantity", "account_restriction")
            return False
        if broker.share_type is ShareType.INTEGER:
            order.quantity = float(int(order.quantity))
        quantity = (
            self.orders.partial_quantities.get(order.order_id, order.quantity)
            if quantity is None
            else quantity
        )
        slippage = calculate_slippage(
            broker.slippage_model,
            order.asset,
            quantity,
            price,
            broker.get_available_size(order.asset, order.side),
        )
        price = price + slippage if order.side is OrderSide.BUY else price - slippage
        if not math.isfinite(price) or price <= 0:
            order.reject("Invalid reservation price", "price_unavailable")
            return False
        commission = calculate_commission(broker.commission_model, order.asset, quantity, price)
        valid, reason = self.validate_fill(order, quantity, price, commission)
        if not valid:
            order.reject(
                reason,
                broker.gatekeeper.classify_rejection(
                    self.account.get_position_quantity(order.asset)
                    + (quantity if order.side is OrderSide.BUY else -quantity)
                ),
            )
            return False
        order._reserved_cash = quantity * price + commission if order.side is OrderSide.BUY else 0.0
        order._reservation_price = price
        return True

    def reserve_remainder(self, order: Order, quantity: float) -> None:
        if quantity <= 0 or order.side is OrderSide.SELL:
            order._reserved_cash = 0.0
            return
        price = order._reservation_price
        # Orders created by internal lifecycle helpers may not have a reservation.
        if price is not None:
            order._reserved_cash = quantity * price + calculate_commission(
                self.broker.commission_model, order.asset, quantity, price
            )

    def hold_sale_proceeds(self, amount: float) -> None:
        timestamp = self.market.time
        assert timestamp is not None
        trade_date = self.session_date(timestamp)
        due = us_equity_settlement_date(trade_date, self.extra_holidays)
        self.account.add_dated_settlement_hold(due, amount)
