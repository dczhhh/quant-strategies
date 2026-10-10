"""Macro schedule time/coverage boundaries and canonical order/fill integration."""

from dataclasses import replace
from datetime import datetime

import pytest
from test_adapter import at, make, tick
from test_corporate_actions import build, held

from ml4t.backtest import OrderStatus, OrderType
from ml4t.backtest.config import DataFrequency
from ml4t.backtest.execution.limits import VolumeParticipationLimit
from quant_constraints import (
    Action,
    ConstraintConfig,
    ConstraintController,
    Holding,
    InMemoryEarningsProvider,
    InMemoryMacroEventProvider,
    Intent,
    Kind,
    MacroEvent,
    MacroEventCoverage,
    MacroEventGate,
    MarketContext,
    RegimeSlippage,
    State,
)
from quant_constraints.calendar import SessionCalendar


def event(kind="CPI", clock="08:30", day="2026-10-09", **changes):
    return MacroEvent(
        **(
            {
                "event_id": kind,
                "event_type": kind,
                "scheduled_at": at(day, clock),
                "scheduled_known_at": at("2026-01-01"),
                "source": "synthetic-official-plan",
            }
            | changes
        )
    )


def provider(*events, **coverage_changes):
    coverage = MacroEventCoverage(
        **(
            {
                "available_at": at("2026-01-01"),
                "covered_from": at("2026-01-01"),
                "covered_until": at("2027-01-01"),
                "source": "synthetic-calendar-coverage",
            }
            | coverage_changes
        )
    )
    return InMemoryMacroEventProvider(events, (coverage,))


def gate(*events, **settings):
    config = replace(ConstraintConfig(), macro_events_enabled=True, **settings)
    return MacroEventGate(config, SessionCalendar(), provider(*events))


def context(clock="10:00", day="2026-10-09", **changes):
    return MarketContext(
        at(day, clock), 100, instrument="plain_sector_etf", sector="tech", **changes
    )


STATE = State(10000, 10000)


@pytest.mark.parametrize(
    "kind,end", [("CPI", "10:30"), ("NFP", "10:30"), ("PCE", "10:30"), ("PPI", "10:15")]
)
@pytest.mark.parametrize("day", ["2026-03-06", "2026-03-09", "2026-10-30", "2026-11-02"])
def test_premarket_morning_blackouts_are_half_open_and_dst_correct(kind, end, day):
    g = gate(event(kind, day=day))
    for clock in ("09:30", "10:00"):
        decision = g.check(Intent("A", 20), STATE, context(clock, day))
        assert decision.action is Action.DEFER and decision.code == "macro_blackout"
        assert datetime.fromisoformat(decision.data["resume_at"]) == at(day, end)
    assert g.check(Intent("A", 20), STATE, context(end, day)).action is Action.ALLOW
    assert g.check(Intent("A", 20), STATE, context("08:30", day)).action is Action.ALLOW


@pytest.mark.parametrize(
    "clock,blocked", [("09:44", False), ("09:45", True), ("10:29", True), ("10:30", False)]
)
def test_ism_timestamp_relative_window(clock, blocked):
    decision = gate(event("ISM", "10:00")).check(Intent("A", 20), STATE, context(clock))
    assert (decision.action is Action.DEFER) is blocked


@pytest.mark.parametrize(
    "clock,blocked",
    [("13:29", False), ("13:30", True), ("14:15", True), ("15:59", True), ("16:00", False)],
)
def test_fomc_statement_and_conference_cover_close_as_union(clock, blocked):
    g = gate(event("FOMC_STATEMENT", "14:00"), event("FOMC_PRESS", "14:30"))
    decision = g.check(Intent("A", 20), STATE, context(clock))
    assert (decision.action is Action.DEFER) is blocked
    if blocked:
        assert datetime.fromisoformat(decision.data["resume_at"]) == at(clock="16:00")
        assert len(decision.data["events"]) == 2


def test_overlap_union_extends_resume_without_duplicating_block():
    # Morning CPI ends 10:30, known ISM at 10:30 extends the union to 11:00.
    decision = gate(event(), event("ISM", "10:30")).check(Intent("A", 20), STATE, context())
    assert datetime.fromisoformat(decision.data["resume_at"]) == at(clock="11:00")
    assert len(decision.data["events"]) == 1  # Only the currently active event.


def test_half_day_clipped_and_holiday_weekend_not_shifted():
    g = gate(event("ISM", "12:50", day="2026-11-27"))
    decision = g.check(Intent("A", 20), STATE, context("12:40", "2026-11-27"))
    assert datetime.fromisoformat(decision.data["resume_at"]) == at("2026-11-27", "13:00")
    g = gate(event(day="2026-07-03"), event("NFP", day="2026-10-10"))
    assert g.check(Intent("A", 20), STATE, context("10:00", "2026-07-06")).action is Action.ALLOW
    assert g.check(Intent("A", 20), STATE, context("10:00", "2026-10-12")).action is Action.ALLOW


def test_optional_fomc_following_session_respects_weekend_and_coverage_lookback():
    g = gate(event("FOMC_STATEMENT", "14:00"), macro_fomc_next_session=True)
    assert g.check(Intent("A", 20), STATE, context("10:00", "2026-10-12")).action is Action.DEFER
    assert g.check(Intent("A", 20), STATE, context("10:30", "2026-10-12")).action is Action.ALLOW
    g.provider = provider(covered_from=at("2026-10-12"))
    assert (
        g.check(Intent("A", 20), STATE, context(day="2026-10-12")).code == "macro_calendar_missing"
    )


def test_future_revision_cancel_and_reschedule_are_visible_only_when_known():
    initial = event()
    revision = replace(
        initial,
        revision=2,
        scheduled_known_at=at(clock="10:05"),
        scheduled_at=at("2026-10-12", "08:30"),
    )
    g = gate(initial, revision)
    assert g.check(Intent("A", 20), STATE, context()).action is Action.DEFER
    assert g.check(Intent("A", 20), STATE, context("10:05")).action is Action.ALLOW
    g.provider = provider(
        initial, replace(initial, revision=2, cancelled=True, scheduled_known_at=at(clock="10:05"))
    )
    assert g.check(Intent("A", 20), STATE, context("10:04")).action is Action.DEFER
    assert g.check(Intent("A", 20), STATE, context("10:05")).action is Action.ALLOW


@pytest.mark.parametrize(
    "coverage_changes",
    [
        {"missing": True},
        {"point_in_time": False},
        {"available_at": at("2026-10-12")},
        {"covered_from": at(clock="10:00")},
        {"covered_until": at(clock="15:00")},
        {"event_types": frozenset({"CPI"})},
    ],
)
def test_coverage_absence_not_equivalent_to_no_events(coverage_changes):
    g = gate()
    g.provider = provider(**coverage_changes)
    assert g.check(Intent("A", 20), STATE, context()).code == "macro_calendar_missing"
    assert gate().check(Intent("A", 20), STATE, context()).action is Action.ALLOW


def test_no_provider_unknown_event_revision_or_missing_event_fail_closed():
    g = MacroEventGate(replace(ConstraintConfig(), macro_events_enabled=True), SessionCalendar())
    assert g.check(Intent("A", 20), STATE, context()).code == "macro_calendar_missing"
    for events in ((event(missing=True),), (event(), event(scheduled_at=at(clock="08:45")))):
        assert (
            gate(*events).check(Intent("A", 20), STATE, context()).code == "macro_calendar_missing"
        )


def test_explicit_missing_optout_and_disabled_never_probe_provider():
    g = gate(missing_macro_calendar="explicit_opt_out")
    g.provider = InMemoryMacroEventProvider()
    assert g.check(Intent("A", 20), STATE, context()).code == "macro_calendar_opt_out"
    disabled = MacroEventGate(ConstraintConfig(), SessionCalendar())

    def fail(_):
        raise AssertionError("Disabled macro feature must not read calendar")

    disabled.provider.snapshot = fail
    assert disabled.check(Intent("A", 20), STATE, context()).action is Action.ALLOW


def test_unannounced_surprise_never_backfills_pre_event_window():
    e = event("ISM", "10:00", scheduled_known_at=at(clock="10:10"), published_at=at(clock="10:10"))
    g = gate(e)
    assert g.check(Intent("A", 20), STATE, context("09:55")).action is Action.ALLOW
    assert g.check(Intent("A", 20), STATE, context("10:10")).action is Action.DEFER
    g = gate(replace(e, published_at=None))
    assert g.check(Intent("A", 20), STATE, context("10:10")).action is Action.ALLOW


@pytest.mark.parametrize("kind", [Kind.REDUCE, Kind.RISK, Kind.REBALANCE])
def test_macro_never_blocks_sales_or_generates_risk_requests(kind):
    g = gate(event())
    assert g.check(Intent("A", -20, kind=kind), STATE, context()).action is Action.ALLOW
    ctrl = ConstraintController(
        replace(ConstraintConfig(), macro_events_enabled=True, drift_reduction="disabled"),
        InMemoryEarningsProvider(),
        provider(event()),
    )
    state = State(8000, 10000, {"A": Holding(20, 100, instrument="plain_sector_etf")})
    assert ctrl.risk_requests(state, context()) == ()


def test_daily_feed_cannot_claim_minute_execution():
    with pytest.raises(ValueError, match="intraday"):
        gate().check(Intent("A", 20), STATE, context(data_frequency="1d"))
    with pytest.raises(ValueError, match="intraday"):
        make(
            settings=replace(ConstraintConfig(), macro_events_enabled=True),
            data_frequency=DataFrequency.DAILY,
        )


def test_macro_defer_does_not_hide_stricter_earnings_rejection():
    from quant_constraints import EarningsEvent

    upcoming = EarningsEvent(
        "A", "earnings", at("2026-10-12", "08:00"), at("2026-01-01"), "BMO", "synthetic-issuer"
    )
    broker, ctrl = make(
        events=(upcoming,), settings=replace(ConstraintConfig(), macro_events_enabled=True)
    )
    ctrl.macro.provider = provider(event())
    tick(broker, at(clock="10:00"))
    order = broker.submit_order("A", 20)
    assert order.status is OrderStatus.REJECTED
    assert ctrl.audit[0].macro_code == "macro_blackout"
    assert ctrl.audit[0].decision.code == "earnings_blackout"
    assert broker.settled_cash == 10000 and order._reserved_cash == 0


def test_unknown_macro_calendar_outweighs_portfolio_resize():
    broker, ctrl = make(settings=replace(ConstraintConfig(), macro_events_enabled=True))
    tick(broker)
    order = broker.submit_order("A", 40)
    assert order.status is OrderStatus.REJECTED
    assert order._rejection_code == "macro_calendar_missing"
    assert order._reserved_cash == 0


def test_macro_calendar_outage_blocks_buy_preserves_sell_cost_path():
    g = gate()

    def outage(_):
        raise OSError("synthetic calendar unavailable")

    g.provider.snapshot = outage
    assert g.check(Intent("A", 20), STATE, context()).code == "macro_calendar_missing"
    assert g.check(Intent("A", -20), STATE, context()).action is Action.ALLOW
    model = RegimeSlippage(g.config, g.calendar, InMemoryEarningsProvider(), macro=g)
    quote = model.quote("A", -20, 100, context())
    assert quote.slippage_bps == 10 and any("missing_calendar=True" in b for b in quote.basis)


def test_surprise_follow_on_cannot_start_before_real_publication():
    e = event(
        "FOMC_STATEMENT",
        "14:00",
        scheduled_known_at=at("2026-10-12", "09:00"),
        published_at=at("2026-10-12", "10:15"),
    )
    g = gate(e, macro_fomc_next_session=True)
    assert g.check(Intent("A", 20), STATE, context("10:00", "2026-10-12")).action is Action.ALLOW
    assert g.check(Intent("A", 20), STATE, context("10:15", "2026-10-12")).action is Action.DEFER


def macro_broker(*events, **changes):
    broker, ctrl = make(settings=replace(ConstraintConfig(), macro_events_enabled=True), **changes)
    ctrl.macro.provider = provider(*events)
    return broker, ctrl


def test_pending_limit_submission_amendment_fill_retry_and_expiry_release_reserve():
    broker, ctrl = macro_broker(event("ISM", "11:00"))
    tick(broker, at(clock="10:30"))
    order = broker.submit_order("A", 20, order_type=OrderType.LIMIT, limit_price=99)
    assert order.status is OrderStatus.PENDING and order._reserved_cash > 0
    tick(broker, at(clock="10:45"), price=99)
    assert not broker.update_order(order.order_id, limit_price=98)
    broker._process_orders(use_open=True)
    assert not broker.fills and order.status is OrderStatus.PENDING
    assert ctrl.audit[-1].decision.code == "macro_blackout" or any(
        a.decision.code == "macro_blackout" for a in ctrl.audit
    )
    tick(broker, at(clock="11:30"), price=99)
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.FILLED and broker.settled_cash == 8020
    assert ctrl.event_statistics()["deferred_entries"] >= 1
    # A new deferred DAY order expires through the existing deadline path.
    broker2, _ = macro_broker(event("FOMC_STATEMENT", "14:00"))
    tick(broker2, at(clock="13:30"))
    waiting = broker2.submit_order("A", 20)
    assert waiting.status is OrderStatus.PENDING
    tick(broker2, at(clock="16:00"))
    assert waiting.status is OrderStatus.CANCELLED and waiting._reserved_cash == 0


def test_partial_remaining_buy_stops_during_blackout_without_reusing_cash():
    broker, _ = macro_broker(event("ISM", "11:00"))
    broker.execution_limits = VolumeParticipationLimit(max_participation=0.001)
    tick(broker, at(clock="10:30"))
    order = broker.submit_order("A", 20)
    tick(broker, at(clock="10:31"))
    broker._process_orders(use_open=True)
    assert order.filled_quantity == 10 and broker.cash == 9000
    tick(broker, at(clock="10:45"))
    broker._process_orders(use_open=True)
    assert order.filled_quantity == 10 and broker.cash == 9000
    tick(broker, at(clock="11:30"))
    broker._process_orders(use_open=True)
    assert order.filled_quantity == 20 and broker.cash == 8000


def test_protective_bracket_and_risk_sale_survive_macro_at_observed_gap_price():
    broker, _ = macro_broker(event())
    tick(broker, at("2026-10-08"))
    parent, profit, stop = broker.submit_bracket("A", 20, stop_loss=95, take_profit=110)
    tick(broker, at("2026-10-08", "10:31"))
    broker._process_orders(use_open=True)
    assert parent.status is OrderStatus.FILLED
    tick(broker, at(clock="09:30"), price=90, opening=90)
    broker._process_orders(use_open=True)
    assert stop.status is OrderStatus.FILLED and profit.status is OrderStatus.CANCELLED
    assert broker.fills[-1].price == 90 and "A" not in broker.positions
    assert broker.unsettled_cash == 1800


@pytest.mark.parametrize("bps", [5.0, 10.0, 20.0])
def test_macro_pressure_max_is_windowed_not_daily_and_reservation_conservative(bps):
    g = gate(event(), macro_slippage_bps=bps)
    model = RegimeSlippage(g.config, g.calendar, InMemoryEarningsProvider(), macro=g)
    stressed = model.quote("A", -20, 100, context("09:30"))
    assert stressed.slippage_bps == bps and stressed.macro_incremental_bps == bps - 2
    ordinary = model.quote("A", -20, 100, context("11:00"))
    assert ordinary.slippage_bps == 2 and ordinary.macro_incremental_bps == 0
    reserve = model.quote("A", 20, 100, context("11:00"), reservation=True)
    assert reserve.slippage_bps == bps


def test_macro_pressure_retains_halfday_and_earnings_max_and_cost_audit():
    g = gate(event(day="2026-11-27"), macro_slippage_bps=2.0)
    model = RegimeSlippage(g.config, g.calendar, InMemoryEarningsProvider(), macro=g)
    assert model.quote("A", -20, 100, context("09:30", "2026-11-27")).slippage_bps == 3
    # Real canonical execution/cost audit, pressure on a permitted sell.
    broker, ctrl, _, _, _ = build(
        settings=replace(ConstraintConfig(), macro_events_enabled=True), costs=True
    )
    ctrl.macro.provider = provider(event("ISM", "11:00"))
    held(broker)
    tick(broker)
    order = broker.submit_order("A", -20)
    tick(broker, at(clock="10:50"))
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.FILLED
    statistics = broker.slippage_statistics()
    assert statistics["event_exposure"] == 1 and statistics["macro_slippage_cost"] == pytest.approx(
        1.6
    )


@pytest.mark.parametrize(
    "change",
    [
        {"macro_major_wait_minutes": -1},
        {"macro_events_enabled": 1},
        {"macro_event_types": ("CPI", "CPI")},
        {"macro_event_types": ("news",)},
        {"macro_slippage_bps": float("nan")},
        {"missing_macro_calendar": "allow"},
    ],
)
def test_invalid_macro_config(change):
    with pytest.raises(ValueError):
        ConstraintConfig(**change)


def test_engine_ab_replays_identical_external_orders_reports_opportunity_cost():
    import polars as pl
    from test_adapter import market

    from ml4t.backtest import DataFeed, Strategy
    from ml4t.backtest.config import CommissionType, SlippageType
    from quant_constraints import cash_backtest_config, constrained_engine

    stamps = [at(clock=clock) for clock in ("10:00", "10:01", "10:30", "10:31", "11:00", "11:01")]
    feed_rows = [
        {
            "timestamp": stamp,
            "asset": "A",
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 10000.0,
        }
        for stamp, price in zip(stamps, [100.0, 100.0, 110.0, 110.0, 100.0, 100.0], strict=True)
    ]
    results = []
    for enabled in (False, True):
        _, ctrl = make(
            settings=replace(
                ConstraintConfig(), macro_events_enabled=enabled, drift_reduction="disabled"
            )
        )
        ctrl.macro.provider = provider(event())

        class ExternalOrderReplay(Strategy):
            def on_data(self, timestamp, data, signals, broker):
                if timestamp == stamps[0]:
                    broker.submit_order("A", 20)
                elif timestamp == stamps[4]:
                    broker.submit_order("A", -20)

        result = constrained_engine(
            DataFeed(prices_df=pl.DataFrame(feed_rows)),
            ExternalOrderReplay(),
            ctrl,
            market,
            cash_backtest_config(
                initial_cash=10000,
                commission_type=CommissionType.NONE,
                commission_per_share=0,
                commission_minimum=0,
                slippage_type=SlippageType.NONE,
                slippage_rate=0,
            ),
        ).run()
        equity = [value for _, value in result.equity_curve]
        peak, drawdown = equity[0], 0.0
        for value in equity:
            peak = max(peak, value)
            drawdown = max(drawdown, 1 - value / peak)
        fills = result.fills
        results.append(
            {
                "return": equity[-1] / equity[0] - 1,
                "max_drawdown": drawdown,
                "turnover": sum(f.quantity * f.price for f in fills) / equity[0],
                "cost": sum(f.commission + abs(f.quantity * f.slippage) for f in fills),
            }
        )
    assert results[0]["return"] == 0 and results[1]["return"] == pytest.approx(-0.02)
    assert results[0]["turnover"] == pytest.approx(0.4)
    assert results[1]["turnover"] == pytest.approx(0.42)
    assert results[0]["max_drawdown"] == pytest.approx(200 / 10200)
    assert results[1]["max_drawdown"] == pytest.approx(0.02)
    assert (
        results[0]["cost"] == results[1]["cost"] == 0
    )  # Isolate gate, not a zero-cost recommendation.
