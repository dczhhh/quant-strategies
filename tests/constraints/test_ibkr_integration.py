"""Default pricing, canonical cash/fills/PnL and all non-mutating entry points."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import polars as pl
import pytest
from test_adapter import ASSETS, ExternalOrders, at, make, market, tick
from test_review_continuation import process

from ml4t.backtest import Broker, DataFeed, OrderStatus, OrderType
from ml4t.backtest.execution.limits import VolumeParticipationLimit
from ml4t.backtest.models import calculate_commission, estimate_commission
from quant_constraints import (
    ConstraintConfig,
    ConstraintController,
    IBKRProTieredUSStock,
    broker_factory,
    cash_backtest_config,
    constrained_engine,
)


def fee_broker(settings=None, provider=market, fee_model=None, initial_cash=10000):
    _, fixture = make()
    ctrl = ConstraintController(settings or ConstraintConfig(), fixture.earnings.provider)
    broker = broker_factory(ctrl, provider, fee_model=fee_model)(
        cash_backtest_config(initial_cash=initial_cash)
    )
    return broker, ctrl


def execute_buy(broker, quantity=20, asset="A", day="2026-10-09"):
    tick(broker, at(day))
    order = broker.submit_order(asset, quantity)
    assert order.status is OrderStatus.PENDING
    process(broker, day)
    assert order.status is OrderStatus.FILLED
    return order


def test_default_yaml_profile_and_100_shares_pay_nonzero_cost():
    path = Path(__file__).parents[2] / "config/us_cash_concentrated.yaml"
    config = ConstraintConfig.from_yaml(path)
    assert config == ConstraintConfig() and config.pricing_plan == "ibkr_pro_tiered"
    # Standalone use is a disclosed nonzero first-tier fallback, not full IBKR.
    standalone = Broker.from_config(cash_backtest_config())
    assert calculate_commission(standalone.commission_model, "A", 100, 100) == pytest.approx(0.35)
    broker, _ = fee_broker(initial_cash=50000)
    order = execute_buy(broker, 100)
    record = broker.fee_records[0]
    assert record.context.order_id == order.order_id and record.context.asset == "A"
    assert record.fees.broker_commission == pytest.approx(0.35)
    assert broker.cash == broker.settled_cash == pytest.approx(40000 - record.fees.total_fees)
    assert broker._execution_journal.fills[0].commission == record.fees.total_fees
    assert broker.fee_statistics()["total_fees"] == record.fees.total_fees


def test_submit_repeat_estimates_amendment_cancel_and_reject_do_not_advance_volume():
    broker, _ = fee_broker()
    tick(broker)
    original = broker.submit_order("A", 20, order_type=OrderType.LIMIT, limit_price=99)
    reserved = broker.reserved_cash
    for _ in range(10):
        broker.estimate_order_fees("A", 20, 99, original.order_id)
        estimate_commission(broker.commission_model, "A", 20, 99)
        deepcopy(broker.commission_model).calculate("A", 20, 99)
    assert broker.fee_model.volume("2026-10") == 0 and broker.fee_records == ()
    assert broker.reserved_cash == reserved
    assert broker.update_order(original.order_id, limit_price=98)
    assert not broker.update_order(original.order_id, quantity=100000)
    assert broker.fee_bridge.generations[original.order_id] == 1
    assert broker.cancel_order(original.order_id)
    bad = broker.submit_order("A", -1)
    assert bad.status is OrderStatus.REJECTED
    assert broker.cash == broker.initial_cash and broker.reserved_cash == 0
    assert broker.fee_model.volume("2026-10") == 0 and broker.fee_records == ()


def test_partial_fills_commit_once_reserve_remaining_and_canceled_remainder_has_no_fee():
    broker, _ = fee_broker()
    tick(broker)
    broker.execution_limits = VolumeParticipationLimit(0.0005)  # five shares per bar
    order = broker.submit_order("A", 20)
    process(broker, "2026-10-09")
    assert order.filled_quantity == 5 and order.status is OrderStatus.PENDING
    assert broker.fee_records[0].fees.broker_commission == 0.35
    assert broker.fee_model.volume("2026-10") == 5
    assert broker.reserved_cash > 1500
    process(broker, "2026-10-09", "10:32")
    assert broker.fee_records[1].fees.broker_commission == 0
    assert order.filled_quantity == 10 and broker.reserved_cash > 1000
    assert broker.cancel_order(order.order_id)
    assert broker.reserved_cash == 0
    fees = broker.fee_statistics()["total_fees"]
    assert broker.cash == pytest.approx(9000 - fees)
    assert broker.fee_model.volume("2026-10") == 10
    # Terminal fill retries cannot recreate a fill or charge a fee.
    assert broker._fill_executor.execute(order, 100)
    assert len(broker.fee_records) == len(broker._execution_journal.fills) == 2
    assert broker.cash == pytest.approx(9000 - fees)


def test_successful_modified_partial_order_gets_new_minimum_failed_amendment_does_not():
    broker, _ = fee_broker()
    tick(broker)
    broker.execution_limits = VolumeParticipationLimit(0.0005)
    order = broker.submit_order("A", 20)
    process(broker, "2026-10-09")
    assert not broker.update_order(order.order_id, quantity=100000)
    assert broker.fee_bridge.generations.get(order.order_id, 0) == 0
    assert broker.update_order(order.order_id, quantity=10)
    process(broker, "2026-10-09", "10:32")
    assert [r.fees.broker_commission for r in broker.fee_records] == [0.35, 0.35]
    assert broker.fee_model.volume("2026-10") == 10


def test_price_only_partial_amendment_preserves_remainder_and_noop_has_no_new_minimum():
    broker, _ = fee_broker()
    tick(broker)
    broker.execution_limits = VolumeParticipationLimit(0.0005)
    order = broker.submit_order("A", 20, order_type=OrderType.LIMIT, limit_price=100)
    process(broker, "2026-10-09")
    assert order.filled_quantity == 5
    assert broker.update_order(order.order_id, limit_price=101)
    assert broker._order_state.partial_quantities[order.order_id] == 15
    assert broker.update_order(order.order_id, limit_price=101)
    assert broker.fee_bridge.generations[order.order_id] == 1
    for clock in ("10:32", "10:33", "10:34"):
        process(broker, "2026-10-09", clock)
    assert order.status is OrderStatus.FILLED and order.filled_quantity == 20
    assert broker.get_position("A").quantity == 20
    assert sum(r.fees.broker_commission for r in broker.fee_records) == pytest.approx(0.7)
    assert broker.fee_model.volume("2026-10") == 20


def test_bracket_children_fee_lifecycles_do_not_charge_dormant_or_canceled_oco():
    broker, _ = fee_broker()
    tick(broker)
    parent, take, stop = broker.submit_bracket("A", 20, stop_loss=95, take_profit=105)
    assert broker.fee_records == ()
    process(broker, "2026-10-09")
    assert parent.status is OrderStatus.FILLED and len(broker.fee_records) == 1
    process(broker, "2026-10-09", "10:32", opening=90)
    assert stop.status is OrderStatus.FILLED and take.status is OrderStatus.CANCELLED
    assert len(broker.fee_records) == 2 and broker.get_position("A") is None
    assert broker.fee_records[-1].context.order_id == stop.order_id
    assert broker.fee_model.volume("2026-10") == 40


def test_gtd_partial_order_next_day_minimum_restarts_without_reusing_funds():
    broker, _ = fee_broker(ConstraintConfig(buy_time_in_force="GTD"))
    tick(broker, at("2026-10-08"))
    broker.execution_limits = VolumeParticipationLimit(0.0005)
    order = broker.submit_order("A", 20)
    process(broker, "2026-10-08")
    process(broker, "2026-10-09")
    assert order.filled_quantity == 10
    assert [r.fees.broker_commission for r in broker.fee_records] == [0.35, 0.35]
    assert broker.reserved_cash > 1000 and broker.fee_model.volume("2026-10") == 10


def test_fractional_execution_has_positive_cost_and_two_stock_sides_share_tiers():
    model = IBKRProTieredUSStock(initial_month="2026-10", initial_monthly_volume=299995)
    broker, _ = fee_broker(fee_model=model)
    execute_buy(broker, 10)
    order = broker.submit_order("B", 10.05)
    process(broker, "2026-10-09", "10:32")
    assert order.status is OrderStatus.FILLED
    assert broker.fee_records[-1].fees.broker_commission == pytest.approx(0.4)
    sale = broker.submit_order("A", -10)
    process(broker, "2026-10-09", "10:33")
    assert sale.status is OrderStatus.FILLED
    assert [r.context.asset for r in broker.fee_records] == ["A", "B", "A"]
    assert [r.context.side for r in broker.fee_records] == ["BUY", "BUY", "SELL"]
    assert broker.fee_records[-1].fees.monthly_volume_before == pytest.approx(300015.05)
    assert broker.fee_model.volume("2026-10") == pytest.approx(300025.05)


@pytest.mark.parametrize("phase", ["submission", "actual_fill"])
def test_higher_fees_cannot_borrow_or_consume_10_percent_reserve(phase):
    broker, ctrl = fee_broker(ConstraintConfig(weekly_entries=10))
    # 75% already invested, then ask for another 15%: fees make exactly 90%
    # infeasible even though the same notional-only request fits.
    tick(broker)
    for asset in ASSETS[:3]:
        broker.submit_order(asset, 24.9)
    process(broker, "2026-10-09")
    quantity = 15
    if phase == "submission":
        broker.fee_model.unknown_venue_per_share = 10
    order = broker.submit_order("D", quantity)
    before = (broker.cash, broker.fee_model.volume("2026-10"), len(broker.fee_records))
    if phase == "actual_fill":
        assert order.status is OrderStatus.PENDING
        broker.fee_model.unknown_venue_per_share = 10
        process(broker, "2026-10-09", "10:32")
    assert order.status is OrderStatus.REJECTED
    assert (broker.cash, broker.fee_model.volume("2026-10"), len(broker.fee_records)) == before
    assert broker.get_position("D") is None and broker.reserved_cash == 0
    assert broker.settled_cash >= 0.1 * broker.get_account_value()
    assert any(
        a.order_id == order.order_id and a.decision.code == "cash_reserve" for a in ctrl.audit
    )


def test_final_rejected_price_or_fees_quote_does_not_consume_tier_or_cash():
    broker, _ = fee_broker()
    tick(broker)
    order = broker.submit_order("A", 25)
    before = (broker.cash, dict(broker.fee_model.monthly_volumes), broker.fee_records)
    process(broker, "2026-10-09", opening=110)
    assert order.status is OrderStatus.REJECTED
    assert (broker.cash, dict(broker.fee_model.monthly_volumes), broker.fee_records) == before
    assert not broker._execution_journal.fills and broker.reserved_cash == 0


def test_round_trip_net_sale_proceeds_cash_pnl_stats_and_t1_release():
    broker, _ = fee_broker()
    execute_buy(broker)
    buy_fee = broker.fee_statistics()["total_fees"]
    settled_before = broker.settled_cash
    sale = broker.submit_order("A", -20)
    process(broker, "2026-10-09", "10:32")
    sale_fee = broker.fee_records[-1].fees.total_fees
    assert sale.status is OrderStatus.FILLED
    assert broker.unsettled_cash == pytest.approx(2000 - sale_fee)
    assert broker.settled_cash == pytest.approx(settled_before)
    assert broker.cash == pytest.approx(10000 - buy_fee - sale_fee)
    trade = broker._execution_journal.trades[-1]
    assert trade.pnl == pytest.approx(-buy_fee - sale_fee)
    assert trade.commission == pytest.approx(buy_fee + sale_fee)
    process(broker, "2026-10-12")  # Columbus Day is not a settlement day
    assert broker.unsettled_cash > 0
    process(broker, "2026-10-13")
    assert broker.unsettled_cash == 0 and broker.settled_cash == broker.cash
    assert sum(
        broker.fee_statistics()[n]
        for n in (
            "broker_commission",
            "exchange_ecn_fees_or_rebates",
            "clearing_fees",
            "regulatory_fees",
            "pass_through_fees",
        )
    ) == pytest.approx(broker.fee_statistics()["total_fees"])


@pytest.mark.parametrize("cycle,due", [("T+1", "2026-11-02"), ("T+2", "2026-11-03")])
def test_cross_settlement_rotation_plan_quotes_are_pure_and_fees_remain_canonical(cycle, due):
    broker, _ = fee_broker(
        ConstraintConfig(
            rebalance_mode="monthly",
            monthly_session="last",
            rebalance_plan_enabled=True,
            settlement_cycle=cycle,
        )
    )
    tick(broker, at("2026-10-01"))
    for asset in ASSETS[:4]:
        broker.submit_order(asset, 22)
    process(broker, "2026-10-01")
    tick(broker, at("2026-10-30"))
    broker.rebalance_to_weights({"B": 0.22, "C": 0.22, "D": 0.22, "E": 0.22}, rebalance_id="priced")
    assert broker.fee_model.volume("2026-10") == 88
    process(broker, "2026-10-30")
    assert broker.get_position("A") is None and broker.get_position("E") is None
    assert broker.unsettled_cash == pytest.approx(2200 - broker.fee_records[-1].fees.total_fees)
    process(broker, "2026-10-30", "10:32")
    assert broker.fee_model.volume("2026-10") == 110
    assert len(broker.fee_records) == 5
    process(broker, due, "10:30")
    assert len(broker.fee_records) == 5 and broker.reserved_cash > 0
    process(broker, due)
    assert broker.rebalance_plans["priced"].status == "completed"
    assert len(broker.fee_records) == len(broker._execution_journal.fills) == 6
    assert broker.fee_model.volume("2026-11") == pytest.approx(broker.get_position("E").quantity)
    equity = broker.get_account_value()
    assert equity == pytest.approx(10000 - broker.fee_statistics()["total_fees"])
    assert broker.settled_cash >= 0.1 * equity


@pytest.mark.parametrize("metadata", ["valid", "future", "missing"])
def test_fee_routing_context_point_in_time_not_future_metadata(metadata):
    def provider(timestamp, asset, phase):
        return replace(
            market(timestamp, asset, phase),
            execution_venue="IEX",
            execution_liquidity="add",
            fee_metadata_available_at=timestamp
            if metadata == "valid"
            else at("2026-10-13")
            if metadata == "future"
            else None,
        )

    broker, _ = fee_broker(provider=provider)
    execute_buy(broker)
    record = broker.fee_records[0]
    assert record.fees.exchange_ecn_fees_or_rebates == pytest.approx(
        0 if metadata == "valid" else 0.07
    )
    assert record.context.venue == ("IEX" if metadata == "valid" else "unknown")


def test_engine_net_value_includes_ibkr_fees_and_keeps_public_fill_schema():
    _, fixture = make()
    ctrl = ConstraintController(ConstraintConfig(), fixture.earnings.provider)
    prices = pl.DataFrame(
        {
            "timestamp": [at(clock=c) for c in ("10:30", "10:31", "10:32")],
            "asset": ["A"] * 3,
            "open": [100.0] * 3,
            "high": [100.0] * 3,
            "low": [100.0] * 3,
            "close": [100.0] * 3,
            "volume": [1000.0] * 3,
        }
    )
    engine = constrained_engine(
        DataFeed(prices_df=prices),
        ExternalOrders(),
        ctrl,
        market,
        cash_backtest_config(initial_cash=10000),
    )
    result = engine.run()
    assert len(engine.broker.fee_records) == len(engine.broker._execution_journal.fills) == 1
    assert engine.broker.get_account_value() == pytest.approx(
        10000 - engine.broker.fee_statistics()["total_fees"]
    )
    assert result is not None


@pytest.mark.parametrize(
    "settings",
    [
        {"pricing_plan": "fixed"},
        {"fee_initial_monthly_volume": 1},
        {"fee_initial_month": "2026-1"},
        {"fee_unknown_venue_per_share": float("nan")},
        {"fee_unknown_venue_rate": -1},
        {"fee_initial_month": 202610},
    ],
)
def test_invalid_fee_config(settings):
    with pytest.raises(ValueError):
        ConstraintConfig(**settings)
