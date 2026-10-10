"""Regression cases requested in the review of PR #4."""

from dataclasses import replace
from datetime import UTC

import pytest

from ml4t.backtest.config import ExecutionPrice
from ml4t.backtest.core.shared import SubmitOrderOptions
from ml4t.backtest.execution.limits import VolumeParticipationLimit
from ml4t.backtest.types import ExecutionMode, OrderStatus
from quant_constraints import (
    Action,
    ConstraintConfig,
    ConstraintController,
    EarningsEvent,
    InMemoryEarningsProvider,
    Intent,
    Kind,
    State,
)
from tests.constraints.test_adapter import ASSETS, at, buy, make, market, tick


@pytest.mark.parametrize(
    ("timing", "announcement", "affected"),
    [
        ("BMO", at("2026-10-14", "07:00"), "2026-10-14"),
        ("AMC", at("2026-10-14", "16:30"), "2026-10-15"),
    ],
)
def test_post_earnings_new_position_never_reuses_pre_earnings_exit(timing, announcement, affected):
    event = EarningsEvent("A", "q3", announcement, at("2026-10-01"), timing, "fixture")
    broker, ctrl = make([event])
    tick(broker, at(affected, "10:30"))
    entry = broker.submit_order("A", 12.5)
    tick(broker, at(affected, "10:31"))
    broker._process_orders(use_open=True)
    assert entry.status is OrderStatus.FILLED
    tick(broker, at(affected, "11:00"))
    broker._process_orders(use_open=True)
    assert broker.get_position("A").quantity == 12.5
    assert len(broker._order_state.orders) == 1
    assert not any(a.decision.code == "earnings_exit_window_missed" for a in ctrl.audit)


def test_late_event_revision_still_exits_a_position_held_before_announcement():
    event = EarningsEvent(
        "A", "q3", at("2026-10-12", "07:00"), at("2026-10-12", "09:45"), "BMO", "revision"
    )
    broker, ctrl = make([event])
    buy(broker)
    tick(broker, at("2026-10-12", "09:45"))
    broker._process_orders(use_open=True)
    assert not broker.get_position("A")
    assert any(a.decision.code == "earnings_exit_window_missed" for a in ctrl.audit)


def test_bracket_parent_arms_both_children_and_oco_exit_leaves_no_naked_position():
    broker, _ = make()
    tick(broker)
    parent, tp, sl = broker.submit_bracket("A", 20, take_profit=110, stop_loss=95)
    assert broker.reserved_cash == 2000
    assert broker.get_pending_orders() == [parent]
    assert tp.parent_id == sl.parent_id == parent.order_id
    tick(broker, at(clock="10:31"))
    broker._process_orders(use_open=True)
    assert parent.status is OrderStatus.FILLED
    assert {o.order_id for o in broker.get_pending_orders()} == {tp.order_id, sl.order_id}
    assert broker.constraint_state().pending_sells["A"] == 20  # OCO is not 40 shares
    assert not broker.cancel_order(sl.order_id)
    tick(broker, at(clock="10:32"), price=94)
    broker._process_orders(use_open=True)
    assert sl.status is OrderStatus.FILLED and tp.status is OrderStatus.CANCELLED
    assert not broker.get_position("A") and not broker.get_pending_orders()


@pytest.mark.parametrize("invalid", ["prices", "parent_reject", "parent_cancel", "fill_reject"])
def test_bracket_invalid_or_unfilled_parent_leaves_neither_exposure_nor_orphans(invalid):
    broker, _ = make()
    tick(broker)
    if invalid == "prices":
        with pytest.raises(ValueError, match="stop_loss"):
            broker.submit_bracket("A", 20, take_profit=99, stop_loss=95)
        assert not broker._order_state.orders
    elif invalid == "parent_reject":
        assert broker.submit_bracket("A", 1, take_profit=110, stop_loss=95) is None
    else:
        parent, tp, sl = broker.submit_bracket("A", 25, take_profit=150, stop_loss=95)
        if invalid == "parent_cancel":
            assert broker.cancel_order(parent.order_id)
        else:
            tick(broker, at(clock="10:31"), price=110)
            broker._process_orders(use_open=True)
            assert parent.status is OrderStatus.REJECTED
        assert tp.status is sl.status is OrderStatus.CANCELLED
    assert not broker.account.positions and not broker.get_pending_orders()
    assert broker.reserved_cash == 0 and broker.cash == 10000


def test_resized_and_immediate_bracket_children_match_actual_parent_fill():
    broker, _ = make(
        execution_mode=ExecutionMode.SAME_BAR,
        execution_price=ExecutionPrice.CLOSE,
        immediate_fill=True,
    )
    tick(broker)
    parent, tp, sl = broker.submit_bracket("A", 30, take_profit=110, stop_loss=95)
    assert parent.filled_quantity == tp.quantity == sl.quantity == 25
    assert broker.get_position("A").quantity == 25
    assert {o.order_id for o in broker.get_pending_orders()} == {tp.order_id, sl.order_id}


def test_partial_bracket_exit_cancels_unfilled_parent_remainder():
    broker, _ = make()
    broker.execution_limits = VolumeParticipationLimit(max_participation=0.0005)
    tick(broker)
    parent, tp, sl = broker.submit_bracket("A", 20, take_profit=110, stop_loss=95)
    tick(broker, at(clock="10:31"))
    broker._process_orders(use_open=True)
    assert parent.filled_quantity == tp.quantity == sl.quantity == 5
    assert broker.constraint_state().pending_sells["A"] == 5
    tick(broker, at(clock="10:32"), price=94)
    broker._process_orders(use_open=True)
    assert parent.status is OrderStatus.CANCELLED
    assert sl.status is OrderStatus.FILLED and tp.status is OrderStatus.CANCELLED
    assert broker.reserved_cash == 0 and not broker.get_position("A")
    tick(broker, at(clock="10:33"), price=100)
    broker._process_orders(use_open=True)
    assert not broker.get_position("A")


def test_immediate_partial_bracket_retains_parent_remainder_and_cash_reservation():
    broker, _ = make(
        execution_mode=ExecutionMode.SAME_BAR,
        execution_price=ExecutionPrice.CLOSE,
        immediate_fill=True,
    )
    broker.execution_limits = VolumeParticipationLimit(max_participation=0.0005)
    tick(broker)
    parent, tp, sl = broker.submit_bracket("A", 20, take_profit=110, stop_loss=95)
    assert parent.status is OrderStatus.PENDING
    assert parent.filled_quantity == tp.quantity == sl.quantity == 5
    assert parent.quantity == 15 and broker.reserved_cash == 1500
    assert {o.order_id for o in broker.get_pending_orders()} == {
        parent.order_id,
        tp.order_id,
        sl.order_id,
    }
    tick(broker, at(clock="10:31"), price=94)
    broker._process_orders(use_open=True)
    assert parent.status is tp.status is OrderStatus.CANCELLED
    assert sl.status is OrderStatus.FILLED
    assert broker.reserved_cash == 0 and not broker.get_pending_orders()
    assert not broker.get_position("A")


def test_cancel_partial_parent_preserves_protection_and_forced_exit_supersedes_children():
    broker, _ = make()
    broker.execution_limits = VolumeParticipationLimit(max_participation=0.0005)
    tick(broker)
    parent, tp, sl = broker.submit_bracket("A", 20, take_profit=110, stop_loss=95)
    tick(broker, at(clock="10:31"))
    broker._process_orders(use_open=True)
    assert broker.cancel_order(parent.order_id)
    assert tp.quantity == sl.quantity == 5
    risk = broker.submit_intent(Intent("A", -5, kind=Kind.RISK))
    assert risk is not tp and risk is not sl
    assert tp.status is sl.status is OrderStatus.CANCELLED
    tick(broker, at(clock="10:32"))
    broker._process_orders(use_open=True)
    assert not broker.get_position("A")


def test_missing_earnings_can_be_explicitly_allowed_but_still_audits_and_respects_known_events():
    config = ConstraintConfig(missing_earnings="allow", missing_earnings_position="hold")
    ctrl = ConstraintController(config, InMemoryEarningsProvider())
    decision = ctrl.check(Intent("A", 20), State(10000, 10000), market(at(), "A", "submission"))
    assert decision.action is Action.ALLOW and decision.code == "earnings_coverage_unverified"
    event = EarningsEvent("A", "q3", at("2026-10-14", "07:00"), at("2026-10-01"), "BMO", "fixture")
    ctrl = ConstraintController(config, InMemoryEarningsProvider((event,)))
    assert (
        ctrl.check(
            Intent("A", 20), State(10000, 10000), market(at("2026-10-13"), "A", "submission")
        ).code
        == "earnings_blackout"
    )


def test_missing_existing_position_calendar_can_hold_or_liquidate():
    for preference in ("hold", "liquidate"):
        broker, ctrl = make(
            settings=ConstraintConfig(
                missing_earnings="allow", missing_earnings_position=preference
            )
        )
        buy(broker)
        ctrl.earnings.provider = InMemoryEarningsProvider()
        tick(broker, at(clock="10:32"))
        broker._process_orders(use_open=True)
        assert bool(broker.get_position("A")) == (preference == "hold")


def test_missing_liquidity_deferred_buy_waits_for_data_and_fills_without_forced_old_exit():
    event = EarningsEvent("A", "q3", at("2026-10-14", "07:00"), at("2026-10-01"), "BMO", "fixture")
    broker, _ = make([event], settings=ConstraintConfig(missing_liquidity="defer"))
    broker.context_provider = lambda stamp, asset, phase: replace(
        market(stamp, asset, phase), rvol=None
    )
    tick(broker, at("2026-10-14", "10:30"))
    order = broker.submit_order("A", 12.5)
    assert order.status is OrderStatus.PENDING
    tick(broker, at("2026-10-14", "10:31"))
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.PENDING and broker.cash == 10000
    broker.context_provider = market
    tick(broker, at("2026-10-14", "10:32"))
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.FILLED
    tick(broker, at("2026-10-14", "10:33"))
    assert broker.get_position("A").quantity == 12.5


@pytest.mark.parametrize("preference", ["error", "defer"])
def test_sparse_held_prices_raise_or_defer_without_stale_execution(preference):
    broker, ctrl = make(settings=ConstraintConfig(missing_price=preference))
    buy(broker)
    if preference == "error":
        with pytest.raises(ValueError, match="mark missing"):
            broker._update_time(
                at(clock="10:32"), {"B": 100}, {"B": 100}, {"B": 100}, {"B": 100}, {"B": 10000}, {}
            )
        return
    broker._update_time(
        at(clock="10:32"), {"B": 100}, {"B": 100}, {"B": 100}, {"B": 100}, {"B": 10000}, {}
    )
    order = broker.submit_order("B", 20)
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.PENDING and not broker.get_position("B")
    assert ctrl.audit[-1].decision.code == "portfolio_marks_missing"
    tick(broker, at(clock="10:33"))
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.FILLED


@pytest.mark.parametrize(("cycle", "settles"), [("T+1", "2026-10-13"), ("T+2", "2026-10-14")])
def test_settlement_and_pending_commitments_never_reuse_cash(cycle, settles):
    broker, _ = make(settings=ConstraintConfig(settlement_cycle=cycle, weekly_entries=20))
    tick(broker)
    for asset in ASSETS[:4]:
        broker.submit_order(asset, 22.5)
    tick(broker, at(clock="10:31"))
    broker._process_orders(use_open=True)
    broker.submit_order("A", -22.5)
    tick(broker, at(clock="10:32"))
    broker._process_orders(use_open=True)
    assert broker.cash == 3250 and broker.settled_cash == 1000 and broker.unsettled_cash == 2250
    assert broker.submit_order("E", 22.5).status is OrderStatus.REJECTED
    for clock in ("10:33", "11:00", "15:00"):
        tick(broker, at(clock=clock))
        assert broker.settled_cash == 1000
    tick(broker, at(settles))
    assert broker.settled_cash == 3250 and broker.unsettled_cash == 0
    pending = broker.submit_order("E", 22.5)
    assert pending.status is OrderStatus.PENDING
    assert broker.submit_order("F", 22.5).status is OrderStatus.REJECTED
    assert broker.cancel_order(pending.order_id)
    replacement = broker.submit_order("F", 22.5)
    assert replacement.status is OrderStatus.PENDING
    tick(broker, at(settles, "10:31"))
    broker._process_orders(use_open=True)
    assert broker.settled_cash == 1000 and broker.unsettled_cash == 0
    for clock in ("10:32", "11:00"):
        tick(broker, at(settles, clock))
        assert broker.settled_cash == 1000


@pytest.mark.parametrize(
    ("mode", "monthly_session"),
    [
        ("monthly", "first"),
        ("monthly", "last"),
        ("semi_monthly", "first"),
        ("every_n_trading_days", "first"),
    ],
)
@pytest.mark.parametrize("timezone", ["ny", "utc"])
def test_rebalance_schedule_same_for_all_external_input_forms(mode, monthly_session, timezone):
    outcomes = []
    for form in ("intent", "target", "weights", "canonical_child"):
        settings = ConstraintConfig(
            rebalance_mode=mode, monthly_session=monthly_session, weekly_entries=20
        )
        broker, _ = make(settings=settings)
        # Observe the anchor session even with zero submissions: all APIs share it.
        tick(broker, at("2026-10-01"))
        for asset, quantity in (("A", 20), ("B", 10), ("C", 10), ("D", 10)):
            broker.submit_order(asset, quantity)
        tick(broker, at("2026-10-01", "10:31"))
        broker._process_orders(use_open=True)
        trace = []
        for day in ("2026-10-02", "2026-10-15", "2026-10-16", "2026-10-30", "2026-11-02"):
            stamp = at(day)
            tick(broker, stamp.astimezone(UTC) if timezone == "utc" else stamp)
            if form == "intent":
                order = broker.submit_intent(
                    Intent("A", 4, kind=Kind.REBALANCE, target_weight=0.24)
                )
            elif form == "target":
                order = broker.order_target_percent("A", 0.24)
            elif form == "weights":
                order = broker.rebalance_to_weights({"A": 0.24, "B": 0.1, "C": 0.1, "D": 0.1})[0]
            else:
                order = broker.submit_order(
                    "A", 4, _options=SubmitOrderOptions(rebalance_id=f"rebalance-{day}")
                )
            trace.append((day, order.status, order.rejection_code))
            if order.status is OrderStatus.PENDING:
                broker.cancel_order(order.order_id)
        outcomes.append(trace)
    assert all(trace == outcomes[0] for trace in outcomes)
    expected_days = {
        "monthly": {"2026-11-02"} if monthly_session == "first" else {"2026-10-30"},
        "semi_monthly": {"2026-10-16", "2026-11-02"},
        "every_n_trading_days": {"2026-10-15"},
    }[mode]
    assert {day for day, status, _ in outcomes[0] if status is OrderStatus.PENDING} == expected_days


@pytest.mark.parametrize("mode", ["monthly", "semi_monthly"])
def test_rebalance_weekend_and_holiday_boundaries_move_to_the_first_session(mode):
    ctrl = ConstraintController(ConstraintConfig(rebalance_mode=mode), InMemoryEarningsProvider())
    state = State(10000, 10000)
    assert not ctrl.rebalance.scheduled(state, at("2027-01-01"))  # NYSE holiday
    assert not ctrl.rebalance.scheduled(state, at("2027-01-02"))  # Saturday
    assert ctrl.rebalance.scheduled(state, at("2027-01-04"))
    if mode == "semi_monthly":
        assert not ctrl.rebalance.scheduled(state, at("2027-01-16"))
        assert not ctrl.rebalance.scheduled(state, at("2027-01-18"))  # MLK Day
        assert ctrl.rebalance.scheduled(state, at("2027-01-19"))


@pytest.mark.parametrize("policy", ["reject", "defer"])
def test_missing_market_data_policy_is_respected_at_submission_and_fill(policy):
    broker, ctrl = make(settings=ConstraintConfig(market_gates=True, missing_market=policy))
    tick(broker)
    order = broker.submit_order("A", 20)
    assert ctrl.audit[-1].decision.code == "market_data_missing"
    if policy == "reject":
        assert order.status is OrderStatus.REJECTED
        assert broker.reserved_cash == 0
    else:
        tick(broker, at(clock="10:31"))
        broker._process_orders(use_open=True)
        assert order.status is OrderStatus.PENDING and broker.cash == 10000
        broker.context_provider = lambda stamp, asset, phase: replace(
            market(stamp, asset, phase), vix=15, vix_available_at=stamp
        )
        tick(broker, at(clock="10:32"))
        broker._process_orders(use_open=True)
        assert order.status is OrderStatus.FILLED


def test_explicit_anchor_does_not_depend_on_when_first_order_arrives():
    settings = ConstraintConfig(rebalance_anchor="2026-10-01")
    ctrl = ConstraintController(settings, InMemoryEarningsProvider())
    assert ctrl.rebalance.scheduled(State(10000, 10000), at("2026-10-15"))
    assert not ctrl.rebalance.scheduled(State(10000, 10000), at("2026-10-16"))
    with pytest.raises(ValueError, match="anchor"):
        ConstraintController(ConstraintConfig(), InMemoryEarningsProvider()).rebalance.scheduled(
            State(10000, 10000), at()
        )


@pytest.mark.parametrize(
    "change",
    [
        {"missing_price": "allow"},
        {"missing_liquidity": "allow"},
        {"missing_earnings_position": "ignore"},
        {"rebalance_anchor": "2026-99-99"},
    ],
)
def test_new_configuration_rejects_unsafe_or_invalid_policies(change):
    with pytest.raises(ValueError):
        ConstraintConfig(**change)
