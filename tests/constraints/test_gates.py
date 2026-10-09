"""Pure policy boundaries, point-in-time inputs and absence of generated buys."""

from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
import yaml

from quant_constraints import (
    Action,
    ConstraintConfig,
    ConstraintController,
    EarningsCoverage,
    EarningsEvent,
    Holding,
    InMemoryEarningsProvider,
    Intent,
    Kind,
    MarketContext,
    State,
)
from quant_constraints.calendar import settlement_date

NY = ZoneInfo("America/New_York")


def at(day="2026-10-09", clock="10:30"):
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=NY)


def provider(events=(), assets=("A",)):
    return InMemoryEarningsProvider(
        tuple(events),
        tuple(
            EarningsCoverage(asset, at("2020-01-01"), at("2030-01-01"), "fixture")
            for asset in assets
        ),
    )


def controller(events=(), **settings):
    return ConstraintController(ConstraintConfig(**settings), provider(events))


def state(**changes):
    return replace(State(10000, 10000, anchor=at()), **changes)


def context(**changes):
    return replace(
        MarketContext(
            at(),
            100,
            sector="technology",
            instrument="equity",
            liquidity_available_at=at(),
            spread=0.001,
            rvol=2,
            volume=1000,
        ),
        **changes,
    )


def check(ctrl=None, intent=None, snapshot=None, market=None):
    return (ctrl or controller()).check(
        intent or Intent("A", 20), snapshot or state(), market or context()
    )


@pytest.mark.parametrize(
    ("day", "clock", "code"),
    [
        ("2026-10-09", "09:29", "outside_rth"),
        ("2026-10-09", "09:30", "entry_window"),
        ("2026-10-09", "09:59", "entry_window"),
        ("2026-10-09", "10:00", "allowed"),
        ("2026-10-09", "15:29", "allowed"),
        ("2026-10-09", "15:30", "entry_window"),
        ("2026-10-09", "16:00", "outside_rth"),
        ("2026-10-10", "10:30", "outside_rth"),
        ("2026-12-25", "10:30", "outside_rth"),
        ("2026-11-27", "12:29", "allowed"),
        ("2026-11-27", "12:30", "entry_window"),
        ("2026-11-27", "13:00", "outside_rth"),
    ],
)
def test_session_boundaries(day, clock, code):
    assert check(market=context(asof=at(day, clock))).code == code


@pytest.mark.parametrize(
    ("stamp", "allowed"),
    [
        (datetime(2026, 3, 6, 15, tzinfo=UTC), True),  # 10 EST
        (datetime(2026, 3, 9, 14, tzinfo=UTC), True),  # 10 EDT
        (datetime(2026, 3, 9, 13, 30, tzinfo=UTC), False),
        (datetime(2026, 11, 2, 15, tzinfo=UTC), True),
    ],
)
def test_dst(stamp, allowed):
    assert (check(market=context(asof=stamp)).action is Action.ALLOW) == allowed


def test_daily_and_naive_data_are_explicit_errors():
    with pytest.raises(ValueError, match="intraday"):
        check(market=context(data_frequency="daily"))
    with pytest.raises(ValueError, match="timezone-aware"):
        check(market=context(asof=datetime(2026, 10, 9, 10)))


@pytest.mark.parametrize(
    ("trade", "cycle", "due"),
    [
        ("2024-05-24", "historical", "2024-05-29"),
        ("2024-05-28", "historical", "2024-05-29"),
        ("2026-10-09", "T+1", "2026-10-13"),
        ("2026-10-09", "T+2", "2026-10-14"),
        ("2026-04-02", "T+1", "2026-04-06"),
        ("2017-09-01", "historical", "2017-09-07"),
    ],
)
def test_settlement_regimes_and_bank_holidays(trade, cycle, due):
    assert settlement_date(date.fromisoformat(trade), cycle) == date.fromisoformat(due)


def test_settlement_extra_closure():
    assert settlement_date(date(2026, 10, 9), holidays=frozenset({date(2026, 10, 13)})) == date(
        2026, 10, 14
    )


def test_cash_reserve_unsettled_and_pending_commitments():
    assert check(snapshot=state(settled_cash=3000)).action is Action.ALLOW
    assert check(snapshot=state(settled_cash=2000)).code == "cash_reserve"
    assert check(snapshot=state(settled_cash=1900)).code == "settled_cash"
    assert check(snapshot=state(reserved_cash=7500)).code == "cash_reserve"
    assert check(market=context(commission=8001)).code == "settled_cash"
    audit = controller()
    check(audit, snapshot=state(settled_cash=1900))
    record = audit.audit[-1]
    assert record.available_settled_cash == 1900 and record.required_funds == 2000
    assert record.order_type == "market" and record.asset == "A" and record.timestamp == at()


def test_risk_exit_priority_same_day_sale_and_no_short():
    holding = Holding(20, 100, "technology", at())
    snapshot = state(settled_cash=0, holdings={"A": holding})
    assert (
        check(intent=Intent("A", -20, kind=Kind.REDUCE), snapshot=snapshot).action is Action.ALLOW
    )
    assert (
        check(
            controller(minimum_hold_sessions=2),
            intent=Intent("A", -20, kind=Kind.REDUCE),
            snapshot=snapshot,
        ).code
        == "minimum_hold"
    )
    assert (
        check(
            controller(minimum_hold_sessions=2),
            Intent("A", -20, kind=Kind.RISK),
            snapshot,
            context(asof=at(clock="09:30")),
        ).action
        is Action.ALLOW
    )
    assert (
        check(
            intent=Intent("A", -20, kind=Kind.RISK),
            snapshot=snapshot,
            market=context(asof=at(clock="18:00")),
        ).action
        is Action.DEFER
    )
    assert (
        check(
            intent=Intent("A", -21, kind=Kind.RISK),
            snapshot=snapshot,
            market=context(asof=at(clock="18:00")),
        ).code
        == "short_sale"
    )
    assert (
        check(
            intent=Intent("A", -20, kind=Kind.RISK),
            snapshot=replace(snapshot, pending_sells={"A": 1}),
        ).code
        == "short_sale"
    )
    assert check(intent=Intent("A", 20, kind=Kind.RISK)).code == "risk_buy_not_allowed"


def event(timing="BMO", announcement=None, availability=None, **kwargs):
    return EarningsEvent(
        "A",
        "q3",
        announcement or at("2026-10-14", "07:00"),
        availability or at("2026-10-01"),
        timing,
        "fixture",
        **kwargs,
    )


@pytest.mark.parametrize(
    ("day", "blocked"), [("2026-10-09", False), ("2026-10-12", True), ("2026-10-13", True)]
)
def test_two_session_blackout(day, blocked):
    decision = check(controller([event()]), market=context(asof=at(day)))
    assert (decision.code == "earnings_blackout") == blocked


def test_bmo_and_amc_liquidation_windows():
    snapshot = state(holdings={"A": Holding(20, 100, "technology")})
    ctrl = controller([event()])
    assert ctrl.risk_requests(snapshot, context(asof=at("2026-10-13", "15:29"))) == ()
    assert (
        ctrl.risk_requests(snapshot, context(asof=at("2026-10-13", "15:30")))[0].reason
        == "earnings_predefined_exit"
    )
    ctrl = controller([event("AMC", at("2026-10-14", "16:30"))])
    assert ctrl.risk_requests(snapshot, context(asof=at("2026-10-13", "15:30"))) == ()
    assert ctrl.risk_requests(snapshot, context(asof=at("2026-10-14", "15:30")))[0].quantity == 20
    assert (
        controller([event()], hold_through_earnings=True).risk_requests(
            snapshot, context(asof=at("2026-10-13", "15:30"))
        )
        == ()
    )


def test_revisions_future_information_and_missing_calendar():
    old = event()
    revision = event(announcement=at("2026-10-20", "07:00"), availability=at("2026-10-13", "12:00"))
    ctrl = controller([old, revision])
    assert check(ctrl, market=context(asof=at("2026-10-13", "11:00"))).code == "earnings_blackout"
    assert check(ctrl, market=context(asof=at("2026-10-13", "12:00"))).action is Action.ALLOW
    future = event(availability=at("2026-10-15"))
    assert check(controller([future]), market=context(asof=at("2026-10-13"))).action is Action.ALLOW
    missing = ConstraintController(ConstraintConfig(), InMemoryEarningsProvider())
    assert check(missing).code == "earnings_coverage_missing"
    missing_provider = InMemoryEarningsProvider(
        coverage=(EarningsCoverage("A", at("2026-10-10"), at("2030-01-01"), "fixture"),)
    )
    assert (
        check(ConstraintController(ConstraintConfig(), missing_provider)).code
        == "earnings_coverage_missing"
    )
    assert (
        check(
            missing, Intent("A", -20, kind=Kind.RISK), state(holdings={"A": Holding(20, 100)})
        ).action
        is Action.ALLOW
    )


@pytest.mark.parametrize("field", ["spread", "rvol", "volume", "liquidity_available_at"])
def test_post_event_missing_liquidity_fails_closed(field):
    assert (
        check(
            controller([event()]),
            Intent("A", 11),
            market=context(asof=at("2026-10-14", "10:30"), **{field: None}),
        ).code
        == "earnings_liquidity_missing"
    )


def test_post_event_wait_spread_rvol_pit_and_half_cap():
    ctrl = controller([event()])
    after = context(asof=at("2026-10-14", "10:30"))
    assert (
        check(ctrl, market=replace(after, asof=at("2026-10-14", "10:29"))).code
        == "post_earnings_wait"
    )
    assert check(ctrl, market=replace(after, spread=0.00151)).code == "earnings_liquidity"
    assert check(ctrl, market=replace(after, rvol=1.79)).code == "earnings_liquidity"
    assert (
        check(ctrl, market=replace(after, liquidity_available_at=at("2026-10-14", "10:31"))).code
        == "earnings_liquidity_missing"
    )
    decision = check(ctrl, market=after)
    assert decision.action is Action.RESIZE and decision.quantity == 12.5
    assert check(ctrl, Intent("A", 12.5), market=after).action is Action.ALLOW
    stats = ctrl.event_statistics()
    assert stats["earnings_gate_triggers"] == 5
    assert stats["buy_checks"] == 6
    amc = controller([event("AMC", at("2026-10-14", "16:30"))])
    assert check(amc, market=context(asof=at("2026-10-15", "10:29"))).code == "post_earnings_wait"
    assert (
        check(amc, Intent("A", 12.5), market=context(asof=at("2026-10-15", "10:30"))).action
        is Action.ALLOW
    )


def test_unknown_timing_and_cancelled_event():
    assert (
        check(controller([event("UNKNOWN")]), market=context(asof=at("2026-10-14"))).code
        == "earnings_timing_unknown"
    )
    cancel = event(availability=at("2026-10-12"), cancelled=True)
    assert (
        check(controller([event(), cancel]), market=context(asof=at("2026-10-13"))).action
        is Action.ALLOW
    )


def test_weekend_announcement_uses_next_affected_session():
    ctrl = controller([event("AMC", at("2026-10-11", "16:30"))])
    known = ctrl.earnings.provider.events[0]
    _, affected, deadline = ctrl.earnings.sessions(known)
    assert affected == date(2026, 10, 12)
    assert deadline.astimezone(NY) == at("2026-10-09", "15:30")
    assert check(ctrl, market=context(asof=at("2026-10-12", "10:29"))).code == "post_earnings_wait"


def test_target_quantity_mismatch_cannot_spoof_minimum_weight():
    assert check(intent=Intent("A", 1, target_weight=0.2)).code == "target_quantity_mismatch"


def test_target_floor_is_not_replenishment_and_drift_is_audited():
    ctrl = controller()
    assert check(ctrl, Intent("A", 9)).code == "target_below_minimum"
    below = state(holdings={"A": Holding(5, 100, "technology")})
    assert ctrl.risk_requests(below, context()) == ()
    assert check(ctrl, Intent("A", 1, kind=Kind.ADD), below).action is Action.ALLOW
    over = state(holdings={"A": Holding(30, 100, "technology")})
    assert check(ctrl, Intent("A", 1, kind=Kind.ADD), over).code == "overweight_drift"
    request = ctrl.risk_requests(over, context())[0]
    assert request.quantity == 5 and request.reason == "overweight_predefined_reduction"
    assert ctrl.audit[-1].decision.code == "overweight_drift"
    assert controller(drift_reduction="disabled").risk_requests(over, context()) == ()


def test_industry_pending_caps_and_instrument_filter():
    holding = {"B": Holding(30, 100, "technology")}
    assert check(snapshot=state(holdings=holding)).action is Action.RESIZE
    assert (
        check(snapshot=state(holdings={"B": Holding(40, 100, "technology")})).code
        == "position_or_industry_cap"
    )
    assert check(snapshot=state(holdings={"B": Holding(20, 100)})).code == "industry_missing"
    assert check(market=context(sector=None)).code == "industry_missing"
    assert (
        check(controller(unknown_industry="warn"), market=context(sector=None)).code
        == "industry_unknown_warning"
    )
    assert check(snapshot=state(pending_buys={"A": 2000})).quantity == 5
    for instrument in (
        "leveraged_etf",
        "inverse_etf",
        "derivative_etf",
        "broad_market_etf",
        "unknown",
    ):
        assert check(market=context(instrument=instrument)).code == "instrument_not_allowed"
    assert check(market=context(instrument="plain_sector_etf")).action is Action.ALLOW


def test_rebalance_schedule_deadband_and_weekly_budget():
    ctrl = controller()
    snapshot = state(holdings={"A": Holding(20, 100, "technology")})
    assert (
        check(ctrl, Intent("A", 3, kind=Kind.REBALANCE, target_weight=0.23), snapshot).action
        is Action.ALLOW
    )
    assert (
        check(ctrl, Intent("A", 2.9, kind=Kind.REBALANCE, target_weight=0.229), snapshot).code
        == "weight_deadband"
    )
    assert not ctrl.rebalance.scheduled(snapshot, at("2026-10-12"))
    assert ctrl.rebalance.scheduled(snapshot, at("2026-10-23"))
    assert (
        check(
            ctrl, Intent("A", -4, kind=Kind.REDUCE), snapshot, context(asof=at("2026-10-12"))
        ).action
        is Action.ALLOW
    )
    assert (
        check(
            ctrl, Intent("A", -4, kind=Kind.RISK), snapshot, context(asof=at("2026-10-12"))
        ).action
        is Action.ALLOW
    )
    assert check(snapshot=state(entry_times=(at(),) * 4)).code == "weekly_entries"
    assert (
        check(snapshot=state(entry_times=(at(),) * 4), market=context(asof=at("2026-10-12"))).action
        is Action.ALLOW
    )
    assert (
        check(snapshot=state(entry_times=(at(),) * 4), market=context(phase="fill")).action
        is Action.ALLOW
    )


@pytest.mark.parametrize(
    ("mode", "day", "scheduled"),
    [
        ("first", "2026-11-02", True),
        ("first", "2026-11-03", False),
        ("last", "2026-11-30", True),
        ("last", "2026-11-27", False),
    ],
)
def test_monthly_schedule(mode, day, scheduled):
    assert (
        controller(rebalance_mode="monthly", monthly_session=mode).rebalance.scheduled(
            state(), at(day)
        )
        == scheduled
    )


def test_market_gates_off_missing_future_vix_and_drawdown_plan():
    assert check().action is Action.ALLOW
    ctrl = controller(market_gates=True)
    assert check(ctrl).code == "market_data_missing"
    market = context(vix=25, vix_available_at=at())
    assert check(ctrl, market=market).quantity == 12.5
    assert check(ctrl, market=replace(market, vix=30)).code == "market_additions_blocked"
    assert (
        check(ctrl, market=replace(market, vix_available_at=at(clock="11:00"))).code
        == "market_data_missing"
    )
    assert (
        check(ctrl, snapshot=state(drawdown=0.1), market=market).code == "market_additions_blocked"
    )
    snapshot = state(drawdown=0.15, holdings={"A": Holding(20, 100, "technology")})
    assert ctrl.risk_requests(snapshot, market) == ()
    assert ctrl.audit[-1].decision.code == "drawdown_reduction_plan_missing"
    plan = controller(market_gates=True, drawdown_reduction_fraction=0.5)
    assert plan.risk_requests(snapshot, market)[0].quantity == 10


def test_complete_external_targets_and_no_automatic_orders():
    ctrl = controller()
    targets = {"A": 0.25, "B": 0.25, "C": 0.2, "D": 0.2}
    sectors = {"A": "one", "B": "two", "C": "three", "D": "four"}
    assert ctrl.check_targets(targets, sectors).action is Action.ALLOW
    assert ctrl.check_targets({"A": 0.25}, sectors).code == "target_name_count"
    assert ctrl.check_targets(targets, dict.fromkeys(targets, "one")).code == "industry_cap"
    assert ctrl.risk_requests(state(), context()) == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"settlement_cycle": "T+0"},
        {"cash_reserve": -0.1},
        {"cash_reserve": float("nan")},
        {"cash_reserve": 0.3},
        {"min_target_weight": 0.3},
        {"rebalance_n": 0},
        {"rebalance_n": True},
        {"weekly_entries": -1},
        {"max_names": 3},
        {"unknown_industry": "ignore"},
        {"hold_through_earnings": "false"},
        {"vix_block_at": 19},
        {"drawdown_reduce": 0.05},
        {"drawdown_reduction_fraction": 0},
    ],
)
def test_config_invalid_values(changes):
    with pytest.raises(ValueError):
        ConstraintConfig(**changes)


def test_sample_config_roundtrip_and_unknown_keys(tmp_path):
    config = ConstraintConfig.from_yaml("config/us_cash_concentrated.yaml")
    assert config == ConstraintConfig()
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(asdict(config)))
    assert ConstraintConfig.from_yaml(path) == config
    path.write_text("typo: 1")
    with pytest.raises(ValueError, match="Unknown"):
        ConstraintConfig.from_yaml(path)
