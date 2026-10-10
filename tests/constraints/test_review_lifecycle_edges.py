"""Execution-time boundaries of authorized allocation plans and deferred orders."""

from dataclasses import replace

import pytest
from test_adapter import ASSETS, at, buy, make, market, tick
from test_gates import context, state
from test_review_continuation import TARGETS, defer_broker, process, rotation, seed

from ml4t.backtest import OrderStatus
from ml4t.backtest.execution.limits import VolumeParticipationLimit
from ml4t.backtest.models import PercentageSlippage
from quant_constraints import Action, ConstraintConfig, ConstraintController, Holding, Intent, Kind


def test_price_drift_that_exhausts_final_cash_budget_cancels_instead_of_borrowing():
    broker, ctrl = rotation()
    process(broker, "2026-10-30")
    process(broker, "2026-11-02", price=101)
    assert broker.rebalance_plans["rotation"].reason == "rebalance_plan_cash_budget_changed"
    assert broker.get_position("E") is None and broker.reserved_cash == 0
    assert any(a.decision.code == "cash_reserve" for a in ctrl.audit)


def test_partial_plan_buy_expires_releasing_only_unfilled_cash():
    broker, ctrl = rotation(rebalance_plan_sessions=2)
    process(broker, "2026-10-30")
    process(broker, "2026-11-02", "10:30")
    broker.execution_limits = VolumeParticipationLimit(0.0005)
    process(broker, "2026-11-02")
    assert broker.get_position("E").quantity == 5 and broker.reserved_cash == 1750
    process(broker, "2026-11-03")
    assert broker.rebalance_plans["rotation"].status == "expired"
    assert broker.reserved_cash == 0 and broker.get_position("E").quantity == 5
    assert broker.settled_cash == 2750
    assert any(a.asset == "E" and a.final_status == "expired" for a in ctrl.audit)


def test_valid_gtd_buy_waits_for_legal_opening_window_and_reports_execution_once():
    broker, ctrl, order = defer_broker(buy_time_in_force="GTD", max_defer_sessions=2)
    accepted = next(a for a in ctrl.audit if a.phase == "order")
    assert accepted.permitted_quantity == 0
    broker.context_provider = lambda t, a, p: replace(market(t, a, p), vix=10, vix_available_at=t)
    process(broker, "2026-10-12", "09:30")
    assert order.status is OrderStatus.PENDING and broker.reserved_cash == 2000
    process(broker, "2026-10-12", "10:00")
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.FILLED
    executed = [a for a in ctrl.audit if a.phase == "execution"]
    assert (
        len(executed) == 1 and executed[0].permitted_quantity == executed[0].filled_quantity == 20
    )
    assert ctrl.event_statistics()["buy_fill_events"] == 1
    assert any(a.phase == "deferred" and a.decision.code == "entry_window" for a in ctrl.audit)


def test_multiday_missing_order_prices_are_audited_and_never_fill_after_expiry():
    broker, ctrl = make(settings=ConstraintConfig(buy_time_in_force="GTD", max_defer_sessions=2))
    tick(broker)
    order = broker.submit_order("A", 20)
    for day in ("2026-10-09", "2026-10-12"):
        broker._update_time(at(day, "11:00"), {}, {}, {}, {}, {}, {})
        broker._process_orders(use_open=True)
        assert order.status is OrderStatus.PENDING
    process(broker, "2026-10-13")
    assert order.status is OrderStatus.CANCELLED and broker.get_position("A") is None
    assert broker.reserved_cash == 0
    assert any(a.phase == "deferred" and a.decision.code == "price_missing" for a in ctrl.audit)


def test_plan_missing_quotes_defer_then_expire_without_stale_execution():
    broker, ctrl = rotation(missing_price="defer", rebalance_plan_sessions=2)
    missing = {a: 100 for a in ASSETS if a != "A"}
    broker._update_time(
        at("2026-10-30", "10:31"),
        missing,
        missing,
        missing,
        missing,
        dict.fromkeys(missing, 10000),
        {},
    )
    broker._process_orders(use_open=True)
    assert broker.get_position("A").quantity == 22.5 and broker.get_position("E") is None
    assert len(broker.rebalance_plans["rotation"].order_ids) == 1
    process(broker, "2026-11-03")
    assert broker.rebalance_plans["rotation"].status == "expired"
    assert broker.get_position("A").quantity == 22.5 and broker.get_position("E") is None
    assert not broker.get_pending_orders()
    assert any(a.final_status == "expired" for a in ctrl.audit)


def test_continuation_rejects_actual_slippage_beyond_plan_price_tolerance():
    broker, ctrl = rotation()
    process(broker, "2026-10-30")
    process(broker, "2026-11-02", "10:30")
    broker.slippage_model = PercentageSlippage(0.06)
    process(broker, "2026-11-02")
    assert broker.rebalance_plans["rotation"].status == "canceled"
    assert broker.get_position("E") is None and broker.reserved_cash == 0
    assert any(
        a.phase == "fill" and a.decision.code == "rebalance_plan_price_changed" for a in ctrl.audit
    )


def test_plan_creation_does_not_authorize_invalid_dates_quotes_or_targets():
    broker, ctrl = make(
        settings=ConstraintConfig(
            rebalance_mode="monthly", monthly_session="last", missing_price="defer"
        )
    )
    tick(broker, at("2026-10-30", "18:00"))
    assert broker.create_rebalance_plan(TARGETS) is None
    assert ctrl.audit[-1].decision.code == "outside_rth"
    tick(broker, at("2026-10-30"))
    with pytest.raises(ValueError, match="nonempty"):
        broker.create_rebalance_plan(TARGETS, rebalance_id="")
    with pytest.raises(ValueError, match="future"):
        broker.create_rebalance_plan(TARGETS, valid_until=at("2026-10-29"))
    assert broker.create_rebalance_plan({"A": 0.25}) is None
    assert ctrl.audit[-1].decision.code == "target_name_count"
    broker._market_state.prices.pop("E")
    assert broker.create_rebalance_plan(TARGETS) is None
    assert ctrl.audit[-1].decision.code == "rebalance_plan_price_missing"


def test_sector_etf_does_not_query_a_company_only_provider_for_holdings():
    class CompanyOnly:
        def snapshot(self, asset, timestamp):
            raise AssertionError("ETF must not query a single-company earnings calendar")

    ctrl = ConstraintController(ConstraintConfig(), CompanyOnly())
    snapshot = state(holdings={"XLK": Holding(20, 100, "technology", at(), "plain_sector_etf")})
    assert ctrl.risk_requests(snapshot, context()) == ()
    assert (
        ctrl.check(Intent("XLK", 20), state(), context(instrument="plain_sector_etf")).action
        is Action.ALLOW
    )


def test_risk_validity_cannot_be_shortened_and_foreign_plan_id_cannot_bypass_gate():
    broker, _ = make()
    buy(broker)
    with pytest.raises(ValueError, match="GTC"):
        broker.submit_intent(Intent("A", -20, kind=Kind.RISK, valid_until=at(clock="11:00")))
    tick(broker, at("2026-10-12"))
    rejected = broker.submit_intent(
        Intent("A", -20, kind=Kind.REBALANCE, target_weight=0, rebalance_id="not-approved")
    )
    assert rejected.rejection_code == "rebalance_schedule"


def test_multiple_rotation_legs_reserve_portfolio_budget_once():
    broker, _ = make(
        settings=ConstraintConfig(
            rebalance_mode="monthly", monthly_session="last", rebalance_plan_enabled=True
        )
    )
    seed(broker)
    tick(broker, at("2026-10-30"))
    targets = dict.fromkeys(("D", "E", "F", "G"), 0.225)
    broker.rebalance_to_weights(targets, rebalance_id="multi")
    process(broker, "2026-10-30")
    assert broker.unsettled_cash == 6750 and broker.get_pending_orders() == []
    process(broker, "2026-11-02", "10:30")
    assert broker.reserved_cash == 6750 and len(broker.get_pending_orders()) == 3
    broker.rebalance_to_weights(targets, rebalance_id="multi")
    assert broker.reserved_cash == 6750 and len(broker.get_pending_orders()) == 3
    process(broker, "2026-11-02")
    assert set(broker.account.positions) == set(targets)
    assert broker.settled_cash == 1000 and broker.reserved_cash == 0
    assert broker.rebalance_plans["multi"].status == "completed"


def test_risk_exit_cancels_continuation_without_reopening_after_settlement():
    broker, ctrl = rotation()
    risk = broker.submit_intent(Intent("A", -22.5, kind=Kind.RISK))
    assert broker.rebalance_plans["rotation"].reason == "rebalance_plan_superseded_by_risk"
    assert broker.get_pending_orders("A") == [risk]
    process(broker, "2026-10-30")
    process(broker, "2026-11-02")
    assert broker.get_position("A") is None and broker.get_position("E") is None
    assert not broker.get_pending_orders()
    assert any(a.decision.code == "rebalance_plan_superseded_by_risk" for a in ctrl.audit)


def test_external_authorized_intent_is_registered_in_the_same_idempotent_plan():
    broker, _ = make(
        settings=ConstraintConfig(
            rebalance_mode="monthly",
            monthly_session="last",
            market_gates=True,
            missing_market="defer",
        )
    )
    tick(broker, at("2026-10-30"))
    plan = broker.create_rebalance_plan(TARGETS, rebalance_id="external")
    assert plan.order_ids == ()
    order = broker.submit_intent(
        Intent("B", 22.5, kind=Kind.REBALANCE, target_weight=0.225, rebalance_id="external")
    )
    assert broker.rebalance_plans["external"].order_ids == (order.order_id,)
    assert broker.order_target_percent("B", 0.225, rebalance_id="external") is order
    broker._process_orders(use_open=True)
    assert broker.reserved_cash == 2250 and len(broker.get_pending_orders()) == 1
    broker.context_provider = lambda t, a, p: replace(market(t, a, p), vix=10, vix_available_at=t)
    process(broker, "2026-11-02", "10:30")
    process(broker, "2026-11-02")
    assert set(broker.account.positions) == set(TARGETS)
    assert all(p.quantity == 22.5 for p in broker.account.positions.values())
    assert broker.reserved_cash == 0 and broker.settled_cash == 1000
    assert len(broker.rebalance_plans["external"].order_ids) == 4
    assert broker.rebalance_plans["external"].status == "completed"
