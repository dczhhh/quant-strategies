"""Cash-account allocation continuation and the second review's lifecycle boundaries."""

from dataclasses import replace
from datetime import datetime

import pytest
from test_adapter import ASSETS, at, buy, make, market, tick
from test_gates import context, controller, state

from ml4t.backtest import OrderStatus
from ml4t.backtest.execution.limits import VolumeParticipationLimit
from quant_constraints import (
    Action,
    ConstraintConfig,
    ConstraintController,
    EarningsEvent,
    Holding,
    InMemoryEarningsProvider,
    Intent,
    Kind,
)

TARGETS = {"B": 0.225, "C": 0.225, "D": 0.225, "E": 0.225}


def seed(broker, assets=ASSETS[:4], weight=0.225):
    tick(broker, at("2026-10-01"))
    for asset in assets:
        assert broker.submit_order(asset, weight * 100).status is OrderStatus.PENDING
    tick(broker, at("2026-10-01", "10:31"))
    broker._process_orders(use_open=True)
    assert set(broker.account.positions) == set(assets)


def process(broker, day, clock="10:31", **prices):
    tick(broker, at(day, clock), **prices)
    broker._process_orders(use_open=True)


@pytest.mark.parametrize("cycle", ["T+1", "T+2"])
@pytest.mark.parametrize(
    ("settings", "creation", "due1", "due2"),
    [
        (
            {"rebalance_mode": "monthly", "monthly_session": "first"},
            "2026-11-02",
            "2026-11-03",
            "2026-11-04",
        ),
        (
            {"rebalance_mode": "monthly", "monthly_session": "last"},
            "2026-10-30",
            "2026-11-02",
            "2026-11-03",
        ),
        ({"rebalance_mode": "semi_monthly"}, "2026-10-16", "2026-10-19", "2026-10-20"),
        (
            {"rebalance_mode": "every_n_trading_days", "rebalance_n": 10},
            "2026-10-15",
            "2026-10-16",
            "2026-10-19",
        ),
    ],
)
def test_authorized_rotation_survives_settlement_and_non_rebalance_day(
    settings, creation, due1, due2, cycle
):
    broker, ctrl = make(
        settings=ConstraintConfig(**settings, settlement_cycle=cycle, rebalance_plan_enabled=True)
    )
    seed(broker)
    tick(broker, at(creation))
    orders = broker.rebalance_to_weights(TARGETS, rebalance_id="rotation")
    assert len(orders) == 1 and orders[0].asset == "A"
    broker._process_orders(use_open=True)
    assert broker.get_position("A") is not None  # NEXT_BAR remains causal
    process(broker, creation)
    assert broker.get_position("A") is None and broker.get_position("E") is None
    assert broker.settled_cash == 1000 and broker.unsettled_cash == 2250
    assert broker.rebalance_plans["rotation"].status == "waiting_cash"
    for minute in ("10:32", "10:33"):
        process(broker, creation, minute)
    assert len(broker.rebalance_plans["rotation"].order_ids) == 1
    if cycle == "T+2":
        process(broker, due1)
        assert broker.get_position("E") is None
    due = due1 if cycle == "T+1" else due2
    process(broker, due, "10:30")
    assert broker.get_position("E") is None  # new continuation also waits NEXT_BAR
    assert broker.reserved_cash == 2250
    pending = broker.get_pending_orders("E")[0]
    assert broker.order_target_percent("E", 0.225, rebalance_id="rotation") is pending
    assert broker.create_rebalance_plan(TARGETS, rebalance_id="new-plan") is None
    assert ctrl.audit[-1].decision.code == "rebalance_schedule"
    process(broker, due)
    assert broker.get_position("E").quantity == 22.5
    assert broker.rebalance_plans["rotation"].status == "completed"
    assert broker.reserved_cash == broker.unsettled_cash == 0
    assert broker.settled_cash == 1000
    repeated = broker.create_rebalance_plan(TARGETS, rebalance_id="rotation")
    assert repeated.status == "completed" and len(repeated.order_ids) == 2
    with pytest.raises(TypeError):
        repeated.targets["E"] = 0.25


def rotation(**settings):
    broker, ctrl = make(
        settings=ConstraintConfig(
            rebalance_mode="monthly",
            monthly_session="last",
            rebalance_plan_enabled=True,
            **settings,
        )
    )
    seed(broker)
    tick(broker, at("2026-10-30"))
    broker.create_rebalance_plan(TARGETS, rebalance_id="rotation")
    return broker, ctrl


def test_settlement_bank_holiday_and_extra_closure_do_not_release_cash_early():
    broker, ctrl = make(
        settings=ConstraintConfig(rebalance_plan_enabled=True), settlement_holidays=["2026-10-13"]
    )
    seed(broker)
    tick(broker, at("2026-10-09"))
    # Explicit observed anchor makes the Friday a legal plan date.
    broker.constraint_anchor = at("2026-10-09")
    broker.create_rebalance_plan(TARGETS, rebalance_id="holiday")
    process(broker, "2026-10-09")
    for day in ("2026-10-10", "2026-10-12", "2026-10-13"):
        process(broker, day)
        assert broker.unsettled_cash == 2250 and broker.get_position("E") is None
    process(broker, "2026-10-14", "10:30")
    process(broker, "2026-10-14")
    assert broker.rebalance_plans["holiday"].status == "completed"
    assert any(a.decision.code == "rebalance_wait_settlement" for a in ctrl.audit)


def test_partial_plan_legs_keep_one_order_and_one_cash_reservation():
    broker, ctrl = rotation()
    broker.execution_limits = VolumeParticipationLimit(0.0005)
    for clock in ("10:31", "10:32", "10:33", "10:34"):
        process(broker, "2026-10-30", clock)
        assert len(broker.rebalance_plans["rotation"].order_ids) == 1
        assert broker.get_position("E") is None
    assert broker.get_position("A").quantity == 2.5
    process(broker, "2026-10-30", "10:35")
    process(broker, "2026-11-02", "10:30")
    assert broker.reserved_cash == 2250
    for clock, remainder in (
        ("10:31", 1750),
        ("10:32", 1250),
        ("10:33", 750),
        ("10:34", 250),
        ("10:35", 0),
    ):
        process(broker, "2026-11-02", clock)
        assert broker.reserved_cash == remainder
        assert len(broker.rebalance_plans["rotation"].order_ids) == 2
    assert broker.rebalance_plans["rotation"].status == "completed"
    stats = ctrl.event_statistics()
    assert stats["buy_fill_events"] == 9  # four seeded orders plus five partial fills
    assert stats["buy_filled_orders"] == 5
    assert stats["buy_filled_quantity"] == 112.5


@pytest.mark.parametrize("cancel_by", ["plan", "order"])
def test_cancel_partial_plan_keeps_actual_position_and_releases_only_remainder(cancel_by):
    broker, ctrl = rotation()
    process(broker, "2026-10-30")
    process(broker, "2026-11-02", "10:30")
    broker.execution_limits = VolumeParticipationLimit(0.0005)
    process(broker, "2026-11-02")
    plan = broker.rebalance_plans["rotation"]
    assert broker.reserved_cash == 1750 and broker.get_position("E").quantity == 5
    assert (
        broker.cancel_rebalance_plan("rotation")
        if cancel_by == "plan"
        else broker.cancel_order(plan.order_ids[-1])
    )
    assert broker.reserved_cash == 0 and broker.settled_cash == 2750
    process(broker, "2026-11-03")
    assert broker.get_position("E").quantity == 5
    assert not broker.get_pending_orders()
    assert broker.rebalance_plans["rotation"].status == "canceled"
    assert not broker.cancel_rebalance_plan("rotation")
    assert any(a.phase == "terminal" and a.final_status == "canceled" for a in ctrl.audit)


def test_plan_expiry_closes_authorization_and_pending_order_before_settlement():
    broker, ctrl = rotation(rebalance_plan_sessions=1)
    process(broker, "2026-10-30")
    process(broker, "2026-11-02")
    assert broker.rebalance_plans["rotation"].status == "expired"
    assert broker.get_position("E") is None and not broker.get_pending_orders()
    assert not broker.constraint_state().authorized_plans
    assert ctrl.audit[-1].decision.code == "rebalance_plan_expired"


@pytest.mark.parametrize("change", ["close", "open", "classification", "earnings", "vix"])
def test_waiting_plan_cancels_when_target_or_observed_market_becomes_illegal(change):
    broker, ctrl = rotation()
    process(broker, "2026-10-30")
    if change == "classification":
        broker.context_provider = lambda t, a, p: replace(
            market(t, a, p), instrument="leveraged_etf" if a == "E" else "equity"
        )
    elif change == "earnings":
        ctrl.earnings.provider = InMemoryEarningsProvider(
            events=(
                EarningsEvent(
                    "E", "next", at("2026-11-04", "07:00"), at("2026-11-02"), "BMO", "revision"
                ),
            ),
            coverage=ctrl.earnings.provider.coverage,
        )
    elif change == "vix":
        ctrl.config = replace(ctrl.config, market_gates=True)
        ctrl.portfolio.config = ctrl.config
        broker.context_provider = lambda t, a, p: replace(
            market(t, a, p), vix=35, vix_available_at=t
        )
    process(
        broker,
        "2026-11-02",
        **({"price": 106} if change == "close" else {"opening": 106} if change == "open" else {}),
    )
    assert broker.rebalance_plans["rotation"].status == "canceled"
    assert broker.get_position("E") is None and broker.reserved_cash == 0
    assert any(
        a.decision.code in {"rebalance_plan_price_changed", "rebalance_plan_target_illegal"}
        for a in ctrl.audit
    )


def test_plan_uses_fresh_price_within_tolerance_and_no_duplicate_external_submission():
    broker, _ = rotation()
    process(broker, "2026-10-30")
    prices = {asset: 101 if asset == "E" else 100 for asset in ASSETS}
    broker._update_time(
        at("2026-11-02"), prices, prices, prices, prices, dict.fromkeys(ASSETS, 10000), {}
    )
    broker._process_orders(use_open=True)
    order = broker.get_pending_orders("E")[0]
    assert order.quantity == pytest.approx(2250 / 101)
    assert (
        broker.submit_intent(
            Intent(
                "E",
                order.quantity,
                kind=Kind.REBALANCE,
                target_weight=0.225,
                rebalance_id="rotation",
            )
        ).status
        is OrderStatus.REJECTED
    )
    broker._update_time(
        at("2026-11-02", "10:31"), prices, prices, prices, prices, dict.fromkeys(ASSETS, 10000), {}
    )
    broker._process_orders(use_open=True)
    assert broker.get_position("E").quantity == pytest.approx(order.filled_quantity)
    assert broker._execution_journal.fills[-1].price == 101


def test_plan_id_cannot_be_retargeted_or_extended():
    broker, _ = rotation()
    with pytest.raises(ValueError, match="different allocation"):
        broker.create_rebalance_plan({**TARGETS, "E": 0.20}, rebalance_id="rotation")
    with pytest.raises(ValueError, match="extended"):
        broker.create_rebalance_plan(TARGETS, rebalance_id="rotation", valid_until=at("2026-11-20"))
    assert broker.order_target_percent("E", 0.25, rebalance_id="rotation") is None
    assert broker.submit_order("F", 10).rejection_code == "rebalance_plan_order_conflict"


@pytest.mark.parametrize("asset", ["XLK", "XLF", "XLE"])
def test_sector_etf_requires_no_company_earnings_but_keeps_liquidity_and_account_gates(asset):
    ctrl = ConstraintController(ConstraintConfig(), InMemoryEarningsProvider())
    ctx = context(instrument="plain_sector_etf")
    intent = Intent(asset, 20)
    assert ctrl.check(intent, state(), ctx).action is Action.ALLOW
    assert ctrl.check(intent, state(settled_cash=2000), ctx).code == "cash_reserve"
    assert ctrl.check(intent, state(), replace(ctx, asof=at(clock="09:30"))).code == "entry_window"
    assert ctrl.check(intent, state(), replace(ctx, volume=None)).code == "liquidity_missing"
    assert ctrl.check(intent, state(), replace(ctx, spread=0.01)).code == "liquidity"
    assert ctrl.check(intent, state(), replace(ctx, sector=None)).code == "industry_missing"
    assert ctrl.check(Intent(asset, 30), state(), ctx).quantity == 25
    holding = Holding(20, 100, "technology", at(), "plain_sector_etf")
    assert ctrl.risk_requests(state(holdings={asset: holding}), ctx) == ()
    assert ctrl.check(Intent("STOCK", 20), state(), context()).code == "earnings_coverage_missing"


@pytest.mark.parametrize(
    "instrument", ["unknown", "leveraged_etf", "inverse_etf", "broad_market_etf"]
)
def test_untrusted_classification_never_receives_etf_exemption(instrument):
    ctrl = ConstraintController(
        ConstraintConfig(missing_earnings_position="hold"), InMemoryEarningsProvider()
    )
    assert (
        ctrl.check(Intent("ETF", 20), state(), context(instrument=instrument)).code
        == "instrument_not_allowed"
    )
    holding = Holding(20, 100, "technology", at(), instrument)
    assert (
        ctrl.risk_requests(state(holdings={"ETF": holding}), context())[0].reason
        == "instrument_metadata_missing_exit"
    )


def test_sector_etf_integration_does_not_get_liquidated_by_missing_company_calendar():
    broker, ctrl = make()
    ctrl.earnings.provider = InMemoryEarningsProvider()
    broker.context_provider = lambda t, a, p: replace(
        market(t, a, p), instrument="plain_sector_etf"
    )
    buy(broker)
    process(broker, "2026-10-12")
    assert broker.get_position("A").quantity == 20 and not broker.get_pending_orders()
    assert not any(a.decision.code.startswith("earnings_") for a in ctrl.audit)


@pytest.mark.parametrize("targets", [{}, {"A": 0.2, "B": 0.2}])
def test_defensive_target_count_is_explicit_and_keeps_allocation_limits(targets):
    ctrl = controller()
    sectors = {a: a for a in targets}
    assert ctrl.check_targets(targets, sectors).code == "target_name_count"
    assert ctrl.check_targets(targets, sectors, defensive_allocation=True).action is Action.ALLOW
    assert (
        controller(allow_defensive_underinvested=True).check_targets(targets, sectors).action
        is Action.ALLOW
    )
    assert (
        ctrl.check_targets({"A": 0.26}, {"A": "one"}, defensive_allocation=True).code
        == "target_weight_range"
    )
    assert (
        ctrl.check_targets(
            {"A": 0.25, "B": 0.25}, {"A": "one", "B": "one"}, defensive_allocation=True
        ).code
        == "industry_cap"
    )
    assert (
        ctrl.check_targets(
            dict.fromkeys("ABCD", 0.25), {a: a for a in "ABCD"}, defensive_allocation=True
        ).code
        == "target_equity_budget"
    )


def test_defensive_six_to_cash_and_two_name_reduction_work_under_buy_block():
    broker, ctrl = make(
        settings=ConstraintConfig(rebalance_plan_enabled=True, weekly_entries=6, market_gates=True)
    )
    broker.context_provider = lambda t, a, p: replace(market(t, a, p), vix=10, vix_available_at=t)
    seed(broker, ASSETS[:6], 0.15)
    broker.context_provider = lambda t, a, p: replace(market(t, a, p), vix=35, vix_available_at=t)
    tick(broker, at("2026-10-15"))
    plan = broker.create_rebalance_plan({}, rebalance_id="defense", defensive_allocation=True)
    assert plan is not None and len(plan.order_ids) == 6
    process(broker, "2026-10-15")
    assert broker.rebalance_plans["defense"].status == "completed"
    assert not broker.account.positions and broker.unsettled_cash == 9000
    assert len([o for o in broker._order_state.orders if o.created_at == at("2026-10-15")]) == 6
    assert not any(
        a.phase == "order" and a.side == "buy" and a.timestamp >= at("2026-10-15")
        for a in ctrl.audit
    )
    other, _ = rotation()
    other.cancel_rebalance_plan("rotation")
    other.controller.portfolio.config = replace(other.controller.config, market_gates=True)
    other.context_provider = lambda t, a, p: replace(market(t, a, p), vix=35, vix_available_at=t)
    defensive = other.create_rebalance_plan(
        {"A": 0.1, "B": 0.1}, rebalance_id="two", defensive_allocation=True
    )
    assert defensive is not None
    process(other, "2026-10-30")
    assert set(other.account.positions) == {"A", "B"}
    assert other.rebalance_plans["two"].status == "completed"


def test_rebalance_sells_do_not_change_earnings_buy_statistics():
    ctrl = controller()
    snapshot = state(holdings={"A": Holding(20, 100, "technology", instrument="equity")})
    ctrl.check(Intent("A", 10), state(), context())
    before = ctrl.event_statistics()
    ctrl.check(Intent("A", -20, kind=Kind.REBALANCE, target_weight=0), snapshot, context())
    assert ctrl.event_statistics() == before
    audit = ctrl.audit[-1]
    assert audit.side == "sell" and audit.requested_quantity == audit.permitted_quantity == 20
    assert audit.filled_quantity == 0 and not audit.earnings_checked
    ctrl.check(Intent("B", 1000), state(), context())
    stats = ctrl.event_statistics()
    assert stats["buy_checks"] == 2 and stats["earnings_buy_checks"] == 1
    assert stats["buy_reject_checks"] == 1 and stats["buy_orders"] == 0


def defer_broker(**settings):
    broker, ctrl = make(
        settings=ConstraintConfig(market_gates=True, missing_market="defer", **settings)
    )
    tick(broker)
    order = broker.submit_order("A", 20)
    assert order.status is OrderStatus.PENDING
    return broker, ctrl, order


@pytest.mark.parametrize("recovery", ["2026-10-12", "2026-10-14"])
def test_day_deferred_order_expires_before_multiday_data_recovery(recovery):
    broker, ctrl, order = defer_broker()
    broker.context_provider = lambda t, a, p: replace(market(t, a, p), vix=10, vix_available_at=t)
    process(broker, recovery)
    assert order.status is OrderStatus.CANCELLED and broker.get_position("A") is None
    assert broker.reserved_cash == 0 and broker.cash == 10000
    assert any(a.phase == "deferred" for a in ctrl.audit)
    assert any(
        a.final_status == "expired" and a.decision.code == "order_expired" for a in ctrl.audit
    )


def test_gtd_counts_exchange_sessions_across_weekend_and_has_hard_limit():
    broker, ctrl, order = defer_broker(buy_time_in_force="GTD", max_defer_sessions=2)
    process(broker, "2026-10-12")
    assert order.status is OrderStatus.PENDING and broker.reserved_cash == 2000
    broker.context_provider = lambda t, a, p: replace(market(t, a, p), vix=10, vix_available_at=t)
    process(broker, "2026-10-13")
    assert order.status is OrderStatus.CANCELLED and broker.get_position("A") is None
    assert ctrl.event_statistics()["buy_fill_events"] == 0


def test_gtd_recovers_before_expiry_and_rechecks_new_earnings_blackout():
    broker, ctrl, order = defer_broker(buy_time_in_force="GTD", max_defer_sessions=3)
    broker.context_provider = lambda t, a, p: replace(market(t, a, p), vix=10, vix_available_at=t)
    ctrl.earnings.provider = InMemoryEarningsProvider(
        events=(
            EarningsEvent(
                "A", "next", at("2026-10-14", "07:00"), at("2026-10-12"), "BMO", "revision"
            ),
        ),
        coverage=ctrl.earnings.provider.coverage,
    )
    process(broker, "2026-10-12")
    assert order.status is OrderStatus.REJECTED and order.rejection_code == "earnings_blackout"
    assert broker.reserved_cash == 0 and broker.get_position("A") is None
    recovery, _, recovered = defer_broker(buy_time_in_force="GTD", max_defer_sessions=3)
    recovery.context_provider = lambda t, a, p: replace(market(t, a, p), vix=10, vix_available_at=t)
    process(recovery, "2026-10-12")
    assert recovered.status is OrderStatus.FILLED


def test_explicit_valid_until_and_half_day_close_are_strict():
    broker, ctrl, first = defer_broker()
    broker.cancel_order(first.order_id)
    order = broker.submit_intent(
        Intent("B", 20, valid_until=at(clock="11:00"), time_in_force="GTD")
    )
    process(broker, "2026-10-09", "11:00")
    assert order.status is OrderStatus.CANCELLED and broker.reserved_cash == 0
    tick(broker, at("2026-11-27"))
    early = broker.submit_order("C", 20)
    assert broker.order_validity[early.order_id] == at("2026-11-27", "13:00")
    process(broker, "2026-11-27", "13:00")
    assert early.status is OrderStatus.CANCELLED
    with pytest.raises(ValueError, match="timezone-aware"):
        broker.submit_order("D", 20, valid_until=datetime(2026, 12, 1))


def test_post_event_defer_expires_at_event_end_even_with_gtd():
    event = EarningsEvent("A", "q3", at("2026-10-14", "07:00"), at("2026-10-01"), "BMO", "fixture")
    broker, ctrl = make(
        [event],
        settings=ConstraintConfig(
            missing_liquidity="defer", buy_time_in_force="GTD", max_defer_sessions=5
        ),
    )
    broker.context_provider = lambda t, a, p: replace(market(t, a, p), rvol=None)
    tick(broker, at("2026-10-14"))
    order = broker.submit_order("A", 10)
    assert order.status is OrderStatus.PENDING
    assert broker.order_validity[order.order_id] == at("2026-10-14", "16:00")
    broker.context_provider = market
    process(broker, "2026-10-15")
    assert order.status is OrderStatus.CANCELLED and broker.get_position("A") is None


def test_missing_prices_expire_buys_but_overnight_risk_waits_for_real_quote():
    broker, ctrl = make(settings=ConstraintConfig(missing_price="defer"))
    buy(broker)
    tick(broker, at(clock="18:00"), price=95)
    risk = broker.submit_intent(Intent("A", -20, kind=Kind.RISK))
    assert risk.order_id not in broker.order_validity
    for day in ("2026-10-12", "2026-10-13"):
        broker._update_time(at(day), {}, {}, {}, {}, {}, {})
        broker._process_orders(use_open=True)
        assert risk.status is OrderStatus.PENDING
    process(broker, "2026-10-14", "09:30", price=80)
    assert risk.status is OrderStatus.FILLED and broker._execution_journal.fills[-1].price == 80
    assert any(a.phase == "deferred" and a.decision.code == "price_missing" for a in ctrl.audit)
    other, _, order = defer_broker()
    other._update_time(at("2026-10-12"), {}, {}, {}, {}, {}, {})
    assert order.status is OrderStatus.CANCELLED


@pytest.mark.parametrize(
    "settings",
    [
        {"buy_time_in_force": "GTC"},
        {"max_defer_sessions": 0},
        {"rebalance_plan_sessions": True},
        {"rebalance_plan_max_price_change": 1.1},
        {"allow_defensive_underinvested": "yes"},
        {"rebalance_plan_enabled": "yes"},
    ],
)
def test_review_lifecycle_configuration_is_validated(settings):
    with pytest.raises(ValueError):
        ConstraintConfig(**settings)
