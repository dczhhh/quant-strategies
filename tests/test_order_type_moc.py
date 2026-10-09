"""Focused tests for market-on-close order behavior."""

from __future__ import annotations

from datetime import datetime

from ml4t.backtest import Broker, OrderSide, OrderType, Position


def test_close_position_can_submit_moc_order():
    broker = Broker()
    broker.positions["AAPL"] = Position(
        asset="AAPL",
        quantity=100.0,
        entry_price=150.0,
        entry_time=datetime(2024, 1, 1, 9, 30),
    )

    order = broker.close_position("AAPL", order_type=OrderType.MOC)

    assert order is not None
    assert order.side is OrderSide.SELL
    assert order.quantity == 100.0
    assert order.order_type is OrderType.MOC
