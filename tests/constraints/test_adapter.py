"""Submission/fill integration and minute-data end-to-end backtest."""

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from ml4t.backtest import DataFeed, OrderStatus, OrderType, Strategy
from ml4t.backtest.config import DataFrequency
from ml4t.backtest.core.shared import SubmitOrderOptions
from ml4t.backtest.models import PerShareCommission
from ml4t.backtest.risk import (
    MaxDrawdownLimit,
    RiskManager,
    StopLoss,
    TakeProfit,
    TimeExit,
    TrailingStop,
)
from ml4t.backtest.risk.types import PositionAction
from quant_constraints import (
    Action,
    ConstraintConfig,
    ConstraintController,
    EarningsCoverage,
    EarningsEvent,
    InMemoryEarningsProvider,
    Intent,
    Kind,
    MarketContext,
    broker_factory,
    cash_backtest_config,
    constrained_engine,
)

NY = ZoneInfo("America/New_York")
ASSETS = ("A", "B", "C", "D", "E", "F", "G")


def at(day="2026-10-09", clock="10:30"):
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=NY)


def market(timestamp, asset, phase):
    return MarketContext(
        timestamp,
        100,
        sector=asset,
        instrument="equity",
        spread=0.001,
        rvol=2,
        volume=1000,
        liquidity_available_at=timestamp,
    )


def make(events=(), settings=None, **changes):
    coverage = tuple(
        EarningsCoverage(asset, at("2020-01-01"), at("2030-01-01"), "fixture") for asset in ASSETS
    )
    controller = ConstraintController(
        settings or ConstraintConfig(), InMemoryEarningsProvider(tuple(events), coverage)
    )
    config = cash_backtest_config(initial_cash=10000, **changes)
    return broker_factory(controller, market)(config), controller


def tick(broker, timestamp=None, price=100, opening=None, close=None):
    broker._update_time(
        timestamp or at(),
        dict.fromkeys(ASSETS, close or price),
        dict.fromkeys(ASSETS, opening or price),
        {asset: max(price, opening or price) for asset in ASSETS},
        {asset: min(price, opening or price) for asset in ASSETS},
        dict.fromkeys(ASSETS, 10000),
        {},
    )


def buy(broker, quantity=20):
    tick(broker)
    order = broker.submit_order("A", quantity)
    tick(broker, at(clock="10:31"))
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.FILLED
    return order


def test_fractional_shares_actual_fill_and_same_day_paid_sale():
    broker, ctrl = make()
    buy(broker, 12.5)
    assert broker.get_position("A").quantity == 12.5
    order = broker.submit_order("A", -12.5)
    tick(broker, at(clock="10:32"))
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.FILLED
    assert broker.cash == 10000 and broker.settled_cash == 8750 and broker.unsettled_cash == 1250
    assert any(a.phase == "fill" and a.decision.action is Action.ALLOW for a in ctrl.audit)


@pytest.mark.parametrize(("cycle", "day"), [("T+1", "2026-10-13"), ("T+2", "2026-10-14")])
def test_configurable_sale_proceeds_settlement(cycle, day):
    broker, _ = make(settings=ConstraintConfig(settlement_cycle=cycle))
    buy(broker)
    broker.submit_order("A", -20)
    tick(broker, at(clock="10:32"))
    broker._process_orders(use_open=True)
    assert broker.unsettled_cash == 2000
    tick(broker, at(day))
    assert broker.unsettled_cash == 0 and broker.settled_cash == 10000


def test_pending_cash_and_sector_reservations_weekly_count_and_cancellation():
    broker, ctrl = make()
    tick(broker)
    for asset in ASSETS[:4]:
        order = broker.submit_order(asset, 20)
        assert order.status is OrderStatus.PENDING
    assert broker.reserved_cash == 8000 and len(broker.entry_times) == 4
    fifth = broker.submit_order("E", 10)
    assert fifth.status is OrderStatus.REJECTED and fifth._rejection_code == "weekly_entries"
    broker.cancel_order("ORD-1")
    assert broker.reserved_cash == 6000
    # An accepted attempt conservatively consumes the weekly budget even if cancelled.
    assert broker.submit_order("G", 10)._rejection_code == "weekly_entries"
    assert len(ctrl.audit) >= 6


def test_actual_gap_and_commission_rejection_are_atomic():
    broker, ctrl = make()
    tick(broker)
    order = broker.submit_order("A", 25)
    tick(broker, at(clock="10:31"), price=110)
    before = (broker.cash, broker.settled_cash, dict(broker.account.positions))
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.REJECTED
    assert order._rejection_code == "position_or_industry_cap"
    assert (broker.cash, broker.settled_cash, dict(broker.account.positions)) == before
    assert broker.reserved_cash == 0
    assert any(
        a.phase == "fill"
        and a.order_id == order.order_id
        and a.decision.code == "position_or_industry_cap"
        for a in ctrl.audit
    )


def test_cash_reserve_is_fraction_of_equity():
    broker, _ = make()
    tick(broker)
    for asset in ASSETS[:4]:
        broker.submit_order(asset, 22.5)
    tick(broker, at(clock="10:31"))
    broker._process_orders(use_open=True)
    assert broker.settled_cash == 1000
    # Cash reserve is a fraction of equity, not a fraction of residual cash.
    assert broker.submit_order("A", 1)._rejection_code == "cash_reserve"


def test_changed_actual_commission_cannot_make_cash_negative():
    broker, ctrl = make()
    tick(broker)
    order = broker.submit_order("A", 20)
    tick(broker, at(clock="10:31"))
    broker.commission_model = PerShareCommission(minimum=9000)
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.REJECTED and order._rejection_code == "settled_cash"
    assert broker.cash == broker.settled_cash == 10000
    assert not broker.account.positions
    assert next(a for a in reversed(ctrl.audit) if a.phase == "fill").required_funds == 11000


def test_core_cash_precheck_rejection_has_structured_audit():
    broker, ctrl = make()
    tick(broker)
    order = broker.submit_order("A", 20)
    tick(broker, at(clock="10:31"), price=1000)
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.REJECTED
    record = next(a for a in reversed(ctrl.audit) if a.phase == "fill_precheck")
    assert record.order_id == order.order_id
    assert record.decision.action is Action.REJECT
    assert record.required_funds == 20000 and record.available_settled_cash == 10000
    assert broker.cash == 10000 and not broker.account.positions


def test_rejected_risk_intent_does_not_cancel_existing_reduction():
    broker, _ = make()
    buy(broker)
    normal = broker.submit_order("A", -5)
    invalid = broker.submit_intent(Intent("A", -21, kind=Kind.RISK))
    assert invalid.status is OrderStatus.REJECTED and invalid._rejection_code == "short_sale"
    assert normal.status is OrderStatus.PENDING and normal in broker.get_pending_orders()
    risk = broker.submit_intent(Intent("A", -20, kind=Kind.RISK))
    assert risk.status is OrderStatus.PENDING and normal.status is OrderStatus.CANCELLED


@pytest.mark.parametrize(
    ("rule", "trigger", "next_price"),
    [
        (TakeProfit(pct=0.05), 110, 105),
        (TimeExit(max_bars=1), 100, 99),
        (TrailingStop(pct=0.05), 90, 85),
    ],
)
def test_existing_exit_rules_defer_to_actual_price(rule, trigger, next_price):
    broker, _ = make()
    buy(broker)
    broker.set_position_rules(rule)
    tick(broker, at(clock="18:00"), price=trigger)
    broker.evaluate_position_rules()
    broker._process_orders(use_open=True)
    assert broker.get_position("A")
    tick(broker, at("2026-10-12", "09:30"), price=next_price)
    broker._process_orders(use_open=True)
    assert not broker.get_position("A")
    assert broker._execution_journal.fills[-1].price == next_price


class DeferredPriceExit:
    def evaluate(self, state):
        return PositionAction.exit_full("predefined_deferred_test", fill_price=95, defer_fill=True)


def test_deferred_rule_cannot_fill_at_old_trigger_after_recovery():
    broker, _ = make()
    buy(broker)
    broker.set_position_rules(DeferredPriceExit())
    tick(broker, at(clock="18:00"), price=94)
    broker.evaluate_position_rules()
    broker._process_pending_exits()
    broker._process_orders(use_open=True)
    assert broker.get_position("A")
    tick(broker, at("2026-10-12", "09:30"), price=110)
    broker._process_pending_exits()
    broker._process_orders(use_open=True)
    assert not broker.get_position("A")
    assert broker._execution_journal.fills[-1].price == 110


def test_max_drawdown_limit_liquidation_composes_with_session_gate():
    broker, _ = make()
    buy(broker)
    manager = RiskManager(limits=[MaxDrawdownLimit(max_drawdown=0.1)])
    manager.update(equity=10000, positions={"A": 2000}, timestamp=at(), broker=broker)
    tick(broker, at(clock="18:00"), price=50)
    manager.update(equity=8900, positions={"A": 1000}, timestamp=at(clock="18:00"), broker=broker)
    broker._process_orders(use_open=True)
    assert broker.get_position("A")
    tick(broker, at("2026-10-12", "09:30"), price=40)
    broker._process_orders(use_open=True)
    assert not broker.get_position("A")
    assert broker._execution_journal.fills[-1].price == 40


def test_complete_external_targets_are_validated_and_do_not_repeat_pending_orders():
    broker, ctrl = make()
    tick(broker)
    assert broker.rebalance_to_weights({"A": 0.9}) == []
    assert ctrl.audit[-1].decision.code == "target_name_count"
    orders = broker.rebalance_to_weights({"A": 0.25, "B": 0.25, "C": 0.2, "D": 0.2})
    assert len(orders) == 4 and all(order.status is OrderStatus.PENDING for order in orders)
    assert broker.rebalance_to_weights({"A": 0.25, "B": 0.25, "C": 0.2, "D": 0.2}) == []
    assert broker.reserved_cash == 9000


def test_amendment_cannot_bypass_cash_portfolio_or_target_floor():
    broker, _ = make()
    tick(broker)
    order = broker.submit_order("A", 20, order_type=OrderType.LIMIT, limit_price=100)
    before = (order.quantity, order._reserved_cash, broker.cash)
    assert not broker.update_order(order.order_id, quantity=90)
    assert not broker.update_order(order.order_id, quantity=5)
    assert (order.quantity, order._reserved_cash, broker.cash) == before
    assert broker.update_order(order.order_id, quantity=22)
    assert broker.reserved_cash == 2200


def test_overnight_stop_uses_next_observed_open_and_keeps_cause():
    broker, ctrl = make()
    buy(broker)
    tick(broker, at(clock="18:00"), price=94)
    order = broker.submit_order(
        "A",
        -20,
        _options=SubmitOrderOptions(
            eligible_in_next_bar_mode=True, risk_exit_reason="stop_loss", risk_fill_price=95
        ),
    )
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.PENDING and broker.get_position("A").quantity == 20
    tick(broker, at("2026-10-12", "09:30"), price=80)
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.FILLED
    assert broker._execution_journal.fills[-1].price == 80
    assert order._risk_exit_reason == "stop_loss" and order._risk_fill_price is None
    assert any(a.decision.action is Action.DEFER for a in ctrl.audit)


def test_existing_stop_rule_can_queue_outside_rth():
    broker, _ = make()
    buy(broker)
    broker.set_position_rules(StopLoss(pct=0.05))
    tick(broker, at(clock="18:00"), price=90)
    broker.evaluate_position_rules()
    broker._process_orders(use_open=True)
    assert broker.get_position("A").quantity == 20
    assert any(order._risk_exit_reason for order in broker._order_state.pending)
    tick(broker, at("2026-10-12", "09:30"), price=80)
    broker._process_orders(use_open=True)
    assert not broker.get_position("A")
    assert broker._execution_journal.fills[-1].price == 80


def test_bmo_forced_exit_before_close_no_automatic_buys():
    event = EarningsEvent("A", "q3", at("2026-10-14", "07:00"), at("2026-10-01"), "BMO", "fixture")
    broker, _ = make([event])
    buy(broker)
    tick(broker, at("2026-10-13", "15:30"))
    broker._process_orders(use_open=True)
    assert not broker.get_position("A")
    assert broker._order_state.orders[-1]._risk_exit_reason == "earnings_predefined_exit"
    tick(broker, at("2026-10-14", "10:30"))
    assert len(broker._order_state.orders) == 2  # gate never creates a post-event buy


def test_late_revision_cannot_fabricate_previous_liquidation():
    event = EarningsEvent("A", "q3", at("2026-10-12", "07:00"), at(clock="18:00"), "BMO", "fixture")
    broker, _ = make([event])
    buy(broker)
    tick(broker, at(clock="18:00"), price=95)
    broker._process_orders(use_open=True)
    assert broker.get_position("A")
    tick(broker, at("2026-10-12", "09:30"), price=80)
    broker._process_orders(use_open=True)
    assert not broker.get_position("A")
    assert broker._execution_journal.fills[-1].price == 80


def test_drift_reduction_waits_until_next_legal_session():
    broker, _ = make()
    buy(broker)
    tick(broker, at(clock="11:00"), price=150)
    broker._process_orders(use_open=True)
    assert broker.get_position("A").quantity == 20
    assert broker.submit_order("A", 1)._rejection_code == "overweight_drift"
    tick(broker, at("2026-10-12", "09:30"), price=150)
    broker._process_orders(use_open=True)
    assert broker.get_position("A").quantity == pytest.approx(18.3333333333)


def test_target_helpers_apply_schedule_and_deadband():
    broker, _ = make()
    buy(broker)
    order = broker.order_target_percent("A", 0.22)
    assert order._rejection_code == "weight_deadband"
    tick(broker, at("2026-10-12", "10:30"))
    assert broker.order_target_percent("A", 0.24)._rejection_code == "rebalance_schedule"
    assert broker.submit_intent(Intent("A", -2, kind=Kind.REDUCE)).status is OrderStatus.PENDING


def test_daily_config_fails_before_strategy_runs():
    with pytest.raises(ValueError, match="intraday"):
        make(data_frequency=DataFrequency.DAILY)


def test_post_earnings_liquidity_is_checked_again_at_fill():
    event = EarningsEvent("A", "q3", at("2026-10-14", "07:00"), at("2026-10-01"), "BMO", "fixture")
    broker, ctrl = make([event])
    tick(broker, at("2026-10-14", "10:30"))
    order = broker.submit_order("A", 12.5)
    assert order.status is OrderStatus.PENDING
    broker.context_provider = lambda stamp, asset, phase: replace(
        market(stamp, asset, phase), rvol=None if phase == "fill" else 2
    )
    tick(broker, at("2026-10-14", "10:31"))
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.REJECTED
    assert order._rejection_code == "earnings_liquidity_missing"
    assert broker.cash == 10000 and not broker.get_position("A")
    assert any(
        a.phase == "fill"
        and a.order_id == order.order_id
        and a.decision.code == "earnings_liquidity_missing"
        for a in ctrl.audit
    )


def test_drawdown_plan_is_applied_once_per_continuous_breach():
    broker, _ = make(settings=ConstraintConfig(market_gates=True, drawdown_reduction_fraction=0.5))
    broker.context_provider = lambda stamp, asset, phase: replace(
        market(stamp, asset, phase), vix=15, vix_available_at=stamp
    )
    buy(broker)
    tick(broker, at(clock="11:00"), price=10)
    broker._process_orders(use_open=True)
    assert broker.get_position("A").quantity == 10
    orders = len(broker._order_state.orders)
    tick(broker, at(clock="11:01"), price=10)
    broker._process_orders(use_open=True)
    assert broker.get_position("A").quantity == 10
    assert len(broker._order_state.orders) == orders


class ExternalOrders(Strategy):
    """Test fixture: predeclared orders, not production buy/sell signals."""

    def __init__(self):
        self.called = False

    def on_data(self, timestamp, data, context, broker):
        if not self.called:
            broker.submit_order("A", 12.5)
            self.called = True


class NoOrders(Strategy):
    def on_data(self, timestamp, data, context, broker):
        pass


def test_minute_engine_end_to_end_and_noop_produces_zero_trades():
    broker, ctrl = make()
    del broker
    stamps = [at(clock=clock) for clock in ("10:30", "10:31", "10:32")]
    prices = pl.DataFrame(
        {
            "timestamp": stamps,
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
    engine.run()
    assert engine.broker.get_position("A").quantity == 12.5
    assert len(engine.broker._execution_journal.fills) == 1
    noop = constrained_engine(
        DataFeed(prices_df=prices),
        NoOrders(),
        ConstraintController(ConstraintConfig(), ctrl.earnings.provider),
        market,
    )
    noop.run()
    assert not noop.broker._execution_journal.fills
    assert noop.broker.cash == noop.broker.initial_cash
