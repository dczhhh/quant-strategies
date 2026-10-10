"""Historical counterfactual costs and causal session/event slippage regressions."""

from dataclasses import replace

import pytest
from test_adapter import ASSETS, at, market, tick
from test_ibkr_tiered import context, filled
from test_review_continuation import process

from ml4t.backtest import Broker, OrderStatus, OrderType
from ml4t.backtest.config import ExecutionPrice, SlippageType
from ml4t.backtest.execution.limits import VolumeParticipationLimit
from ml4t.backtest.models import calculate_slippage
from quant_constraints import (
    ConstraintConfig,
    ConstraintController,
    EarningsCoverage,
    EarningsEvent,
    IBKRProTieredUSStock,
    InMemoryEarningsProvider,
    Intent,
    Kind,
    RegimeSlippage,
    broker_factory,
    cash_backtest_config,
)
from quant_constraints.calendar import SessionCalendar, settlement_date
from quant_constraints.fees.ibkr_tiered import BACKCAST_REGULATORY_RATE


def provider(events=(), *, missing=False):
    coverage = tuple(
        EarningsCoverage(asset, at("1995-06-07"), at("2030-01-01"), "PIT fixture", missing)
        for asset in ASSETS
    )
    return InMemoryEarningsProvider(tuple(events), coverage)


def event(day="2026-10-09", clock="08:00", timing="BMO", known=None, asset="A"):
    return EarningsEvent(
        asset, "release", at(day, clock), known or at(day, clock), timing, "issuer release"
    )


def cost_broker(events=(), settings=None, cash=10000, market_provider=market, fee_model=None):
    controller = ConstraintController(settings or ConstraintConfig(), provider(events))
    broker = broker_factory(controller, market_provider, fee_model=fee_model)(
        cash_backtest_config(initial_cash=cash)
    )
    return broker, controller


def quote_at(
    stamp, events=(), *, settings=None, instrument="equity", earnings=None, reservation=False
):
    model = RegimeSlippage(
        settings or ConstraintConfig(), SessionCalendar(), earnings or provider(events)
    )
    quote = model.quote(
        "A",
        100,
        100,
        replace(market(stamp, "A", "slippage"), instrument=instrument),
        reservation=reservation,
    )
    return model, quote


@pytest.mark.parametrize("year", [2005, 2015, 2025, 2026])
def test_backcast_uses_one_snapshot_real_months_and_nonholiday_taf(year):
    model = IBKRProTieredUSStock(history_mode="current_snapshot_backcast")
    first = filled(model, "buy", 1000, ctx=context("buy", f"{year}-10-01"))
    sell = filled(model, "sell", 1000, ctx=context("sell", f"{year}-10-02", "SELL"))
    new_month = filled(model, "nov", 1000, ctx=context("nov", f"{year}-11-02"))
    assert [r.fees.broker_commission for r in (first, sell, new_month)] == [3.5] * 3
    assert sell.fees.taf_fees == pytest.approx(0.195)
    assert sell.fees.sec_fees == pytest.approx(2.06)
    assert sell.fees.cat_fees == pytest.approx(0.003)
    assert model.monthly_volumes == {f"{year}-10": 2000, f"{year}-11": 1000}
    for record in model.records:
        assert record.fees.historical_fee_proxy is True
        assert record.fees.fee_snapshot_date == "2026-10-10"
        assert "normal-nonholiday-proxy" in record.fees.rate_version
        assert record.fees.sources and any("counterfactual" in s for s in record.fees.assumptions)
    assert new_month.fees.monthly_volume_before == 0


@pytest.mark.parametrize("day", ["2005-10-03", "2015-10-01", "2025-10-01", "2027-01-04"])
def test_strict_history_remains_fail_closed(day):
    model = IBKRProTieredUSStock(history_mode="strict_historical")
    with pytest.raises(ValueError, match="No explicit regulatory"):
        filled(model, "missing", 100, ctx=context("missing", day))
    assert model.records == () and model.monthly_volumes == {}


def test_backcast_does_not_propagate_q4_holiday_and_fee_sensitivity_is_explicit():
    ctx = context("sale", side="SELL")
    strict = IBKRProTieredUSStock(history_mode="strict_historical").quote(ctx, 100, 100)
    base = IBKRProTieredUSStock(history_mode="current_snapshot_backcast").quote(ctx, 100, 100)
    stress = IBKRProTieredUSStock(
        history_mode="current_snapshot_backcast",
        backcast_regulatory_rate=replace(
            BACKCAST_REGULATORY_RATE,
            taf_per_share=0.00039,
            sec_per_dollar=0.0000412,
            version="regulatory-2x-sensitivity",
            sources=("explicit doubled proxy scenario",),
        ),
    ).quote(ctx, 100, 100)
    assert strict.fees.taf_fees == 0 and not strict.fees.historical_fee_proxy
    assert base.fees.taf_fees > 0
    assert stress.fees.regulatory_fees > base.fees.regulatory_fees > strict.fees.regulatory_fees
    assert (
        stress.fees.broker_commission
        == base.fees.broker_commission
        == strict.fees.broker_commission
    )
    assert stress.fees.rate_version == "regulatory-2x-sensitivity"


@pytest.mark.parametrize(
    ("day", "due"),
    [
        ("2005-10-03", "2005-10-06"),
        ("2015-10-01", "2015-10-06"),
        ("2025-10-01", "2025-10-02"),
        ("2026-10-09", "2026-10-13"),
    ],
)
def test_historical_factory_fills_and_settles_by_execution_date(day, due):
    broker, _ = cost_broker()
    tick(broker, at(day))
    buy = broker.submit_order("A", 20)
    process(broker, day)
    assert buy.status is OrderStatus.FILLED and buy.filled_price == pytest.approx(100.02)
    sale = broker.submit_order("A", -20)
    process(broker, day, "10:32")
    assert sale.status is OrderStatus.FILLED
    net = 20 * 99.98 - broker.fee_records[-1].fees.total_fees
    assert broker.unsettled_cash == pytest.approx(net)
    assert settlement_date(at(day).date()).isoformat() == due
    tick(broker, at(due))
    assert broker.unsettled_cash == 0 and broker.settled_cash == broker.cash
    assert broker.fee_model.monthly_volumes == {day[:7]: 40}
    assert all(r.fees.historical_fee_proxy for r in broker.fee_records)


@pytest.mark.parametrize(
    ("stamp", "events", "regime", "bps"),
    [
        (at(), (), "regular", 2),
        (at("2026-11-27"), (), "early_close", 3),
        (at(), (event(),), "earnings_affected", 5),
        (
            at("2026-10-08", "12:00"),
            (event("2026-10-08", "16:05", "AMC", at("2026-10-01")),),
            "regular",
            2,
        ),
        (at(), (event("2026-10-08", "16:05", "AMC"),), "earnings_affected", 5),
        (
            at("2026-10-09", "10:45"),
            (event(clock="10:30", timing="DURING", known=at(clock="11:00")),),
            "regular",
            2,
        ),
        (
            at("2026-10-09", "11:00"),
            (event(clock="10:30", timing="DURING", known=at(clock="11:00")),),
            "earnings_affected",
            5,
        ),
        (at("2026-10-13"), (event("2026-10-11", "08:00"),), "regular", 2),
        (at("2026-10-12"), (event("2026-10-11", "08:00"),), "earnings_affected", 5),
        (at("2026-11-27"), (event("2026-11-26", "08:00"),), "earnings_affected", 5),
        (
            at("2026-11-27", "12:00"),
            (event("2026-11-27", "13:05", "AMC", at("2026-11-01")),),
            "early_close",
            3,
        ),
        (at("2026-11-30"), (event("2026-11-27", "13:05", "DURING"),), "earnings_affected", 5),
        (at("2026-11-27"), (event("2026-11-27"),), "earnings_affected", 5),
        (at("2026-03-09"), (event("2026-03-06", "16:05", "AMC"),), "earnings_affected", 5),
        (at("2026-11-02"), (event("2026-10-30", "16:05", "AMC"),), "earnings_affected", 5),
    ],
)
def test_pit_release_mapping_real_sessions_dst_and_max_overlap(stamp, events, regime, bps):
    model, quote = quote_at(stamp, events)
    assert quote.slippage_regime == regime and quote.slippage_bps == bps
    assert quote.per_share == pytest.approx(100 * bps / 10000)
    assert quote.slippage_amount == pytest.approx(bps)
    assert quote.basis and model.records == ()
    if regime == "earnings_affected":
        assert any("announcement_at=" in s and "available_at=" in s for s in quote.basis)


def test_future_revisions_and_future_announcements_cannot_change_past_quote():
    original = event("2026-10-08", "16:05", "AMC")
    revision = replace(original, announcement_at=at("2026-10-20"), available_at=at("2026-10-10"))
    _, baseline = quote_at(at(), (original,))
    _, revised = quote_at(at(), (original, revision))
    assert revised == baseline
    _, future = quote_at(at(), (event("2026-10-20", known=at("2026-10-01")),))
    assert future.slippage_bps == 2

    # Boundary enforcement also protects against an external provider leaking
    # future records, rather than trusting that provider's filtering.
    class LeakyProvider:
        def snapshot(self, asset, asof):
            return provider().snapshot(asset, asof)[0], (revision,)

    _, leaky = quote_at(at(), earnings=LeakyProvider())
    assert leaky.slippage_bps == 2


@pytest.mark.parametrize("missing", ["stress", "regular", "error"])
def test_missing_earnings_behavior_explicit_and_audited(missing):
    settings = ConstraintConfig(slippage_missing_earnings=missing)
    if missing == "error":
        with pytest.raises(ValueError, match="Missing point-in-time"):
            quote_at(at(), settings=settings, earnings=provider(missing=True))
    else:
        _, quote = quote_at(at(), settings=settings, earnings=provider(missing=True))
        assert quote.slippage_bps == (5 if missing == "stress" else 2)
        assert any("configured fallback=" + missing in s for s in quote.basis)


def test_etf_classification_no_issuer_stress_and_calendar_fallback_is_audited():
    _, quote = quote_at(at("2026-11-27"), (event("2026-11-27"),), instrument="plain_sector_etf")
    assert quote.slippage_regime == "early_close" and quote.slippage_bps == 3
    _, unknown = quote_at(at(), instrument="unknown")
    assert unknown.slippage_regime == "earnings_fallback"
    _, no_session = quote_at(at("2026-10-10"), instrument="plain_sector_etf")
    assert no_session.slippage_regime == "calendar_fallback"
    assert any("no current NYSE session" in s for s in no_session.basis)


@pytest.mark.parametrize("bps", [0, 1, 2, 3, 5, 10])
def test_regime_bps_scenarios_and_bound_upstream_protocol(bps):
    settings = ConstraintConfig(slippage_regular_bps=bps)
    model, quote = quote_at(at(), settings=settings)
    with model.bind(market(at(), "A", "slippage"), actual=True):
        assert calculate_slippage(model, "A", 100, 100, 1000) == pytest.approx(bps / 100)
        assert model.actual_quote == quote
    assert model.actual_quote is None and model.records == ()
    with pytest.raises(ValueError, match="bound execution context"):
        model.calculate("A", 100, 100, 1000)


@pytest.mark.parametrize(
    ("day", "events", "bps", "regime"),
    [
        ("2026-10-09", (), 2, "regular"),
        ("2026-11-27", (), 3, "early_close"),
        ("2026-10-09", (event(),), 5, "earnings_affected"),
    ],
)
def test_actual_buy_sell_prices_fees_audit_and_separate_totals(day, events, bps, regime):
    broker, controller = cost_broker(events, cash=100000)
    tick(broker, at(day))
    buy = broker.submit_order("A", 100)
    assert broker.reserved_cash > 10000
    assert broker.fee_records == broker.slippage_records == ()
    process(broker, day)
    sale = broker.submit_order("A", -100)
    process(broker, day, "10:32")
    assert buy.status is sale.status is OrderStatus.FILLED
    assert buy.filled_price == pytest.approx(100 + bps / 100)
    assert sale.filled_price == pytest.approx(100 - bps / 100)
    assert [r.price for r in broker.fee_records] == [buy.filled_price, sale.filled_price]
    assert broker.fee_records[-1].fees.sec_fees == pytest.approx(
        100 * sale.filled_price * 0.0000206
    )
    stats = broker.execution_cost_statistics()
    scenario = stats["fee_scenarios"][0]
    assert scenario["historical_fee_proxy"] is True
    assert scenario["fee_snapshot_date"] == "2026-10-10" and scenario["executions"] == 2
    assert scenario["sources"] and scenario["assumptions"]
    assert stats["slippage"]["total_slippage"] == pytest.approx(2 * bps)
    assert stats["slippage"]["by_regime"][regime] == {
        "fills": 2,
        "quantity": 200,
        "slippage_amount": pytest.approx(2 * bps),
    }
    assert broker.cash == pytest.approx(100000 - stats["fees"]["total_fees"] - 2 * bps)
    executions = [a for a in controller.audit if a.phase == "execution"]
    assert len(executions) == 2
    assert all(a.slippage_regime == regime and a.slippage_bps == bps for a in executions)
    assert all(a.slippage_basis and a.slippage_amount == pytest.approx(bps) for a in executions)


def test_submit_regular_then_public_during_fill_stress_with_conservative_reservation():
    broker, _ = cost_broker((event(clock="10:31", timing="DURING"),))
    tick(broker)
    order = broker.submit_order("A", 10)
    _, current = quote_at(at(), (event(clock="10:31", timing="DURING"),))
    assert current.slippage_bps == 2
    assert order._reservation_price == pytest.approx(100.05)
    reserved = broker.reserved_cash
    process(broker, "2026-10-09")
    assert order.status is OrderStatus.FILLED and order.filled_price == pytest.approx(100.05)
    assert 10000 - broker.cash <= reserved
    assert broker.fee_model.volume("2026-10") == 10
    assert broker.slippage_records[0].quote.slippage_bps == 5


@pytest.mark.parametrize("cap", ["single", "industry", "cash"])
def test_new_stress_and_actual_open_recheck_hard_limits_before_cost_commit(cap):
    settings = ConstraintConfig(hold_through_earnings=True, event_position_multiplier=1)
    stress_event = event(clock="10:32", timing="DURING", asset="D" if cap == "cash" else "B")
    broker, controller = cost_broker((stress_event,), settings)
    tick(broker)
    if cap == "single":
        target, quantity, opening = "B", 24.98, 100.1
    elif cap == "industry":

        def same_sector(timestamp, asset, phase):
            return replace(market(timestamp, asset, phase), sector="shared")

        broker.context_provider = same_sector
        broker.submit_order("A", 19.9)
        process(broker, "2026-10-09")
        target, quantity, opening = "B", 20, 100.8
    else:
        for asset in ("A", "B", "C"):
            broker.submit_order(asset, 22)
        process(broker, "2026-10-09")
        target, quantity, opening = "D", 23.9, 100.5
    before = (broker.cash, len(broker.fee_records), broker.fee_model.volume("2026-10"))
    order = broker.submit_order(target, quantity)
    assert order.status is OrderStatus.PENDING
    process(broker, "2026-10-09", "10:32", opening=opening)
    assert order.status is OrderStatus.REJECTED
    assert (broker.cash, len(broker.fee_records), broker.fee_model.volume("2026-10")) == before
    assert len(broker.slippage_records) == before[1]
    assert order._reserved_cash == 0
    codes = {a.decision.code for a in controller.audit if a.order_id == order.order_id}
    expected = {
        "single": "earnings_position_cap",
        "industry": "position_or_industry_cap",
        "cash": "cash_reserve",
    }
    assert expected[cap] in codes


def test_partial_stress_bracket_retains_protection_and_canceled_remainder_has_no_cost():
    broker, _ = cost_broker((event(clock="10:31", timing="DURING"),))
    tick(broker)
    broker.execution_limits = VolumeParticipationLimit(0.0005)
    parent, tp, stop = broker.submit_bracket("A", 10, take_profit=110, stop_loss=90)
    process(broker, "2026-10-09")
    assert parent.filled_quantity == 5 and parent.status is OrderStatus.PENDING
    assert tp.quantity == stop.quantity == 5 and broker.reserved_cash > 500.25
    assert not broker.cancel_order(stop.order_id)
    assert broker.cancel_order(parent.order_id)
    assert tp.status is stop.status is OrderStatus.PENDING
    process(broker, "2026-10-09", "10:32", opening=80, price=80)
    assert stop.status is OrderStatus.FILLED and tp.status is OrderStatus.CANCELLED
    assert stop.filled_price == pytest.approx(79.96)
    assert broker.get_position("A") is None and broker.reserved_cash == 0
    assert [r.quote.slippage_regime for r in broker.slippage_records] == ["earnings_affected"] * 2
    assert broker.fee_model.volume("2026-10") == 10
    assert len(broker.fee_records) == 2
    assert broker._fill_executor.execute(stop, 80)
    assert len(broker.slippage_records) == 2


def test_overnight_gtc_risk_uses_observed_gap_open_and_fill_day_earnings_regime():
    settings = ConstraintConfig(hold_through_earnings=True)
    broker, _ = cost_broker((event("2026-10-09", "16:05", "AMC"),), settings)
    tick(broker, at("2026-10-01"))
    broker.submit_order("A", 20)
    process(broker, "2026-10-01")
    tick(broker, at("2026-10-09", "17:00"), price=90)
    order = broker.submit_intent(Intent("A", -20, kind=Kind.RISK))
    assert order.status is OrderStatus.PENDING and order.order_id not in broker.order_validity
    process(broker, "2026-10-10", opening=80, price=80)
    assert order.status is OrderStatus.PENDING and len(broker.fee_records) == 1
    process(broker, "2026-10-12", "09:30", opening=70, price=70)
    assert order.status is OrderStatus.FILLED and order.filled_price == pytest.approx(69.965)
    assert broker.slippage_records[-1].quote.slippage_regime == "earnings_affected"
    assert broker.unsettled_cash == pytest.approx(
        20 * 69.965 - broker.fee_records[-1].fees.total_fees
    )


def test_preannouncement_routine_liquidation_regular_and_postevent_new_entry_not_cleared():
    broker, _ = cost_broker((event("2026-10-09", "16:05", "AMC", at("2026-10-01")),))
    tick(broker, at("2026-10-01"))
    broker.submit_order("A", 20)
    process(broker, "2026-10-01")
    process(broker, "2026-10-09", "15:30")
    assert broker.get_position("A") is None
    assert broker.slippage_records[-1].quote.slippage_regime == "regular"
    tick(broker, at("2026-10-12", "10:30"))
    post = broker.submit_order("A", 10)
    process(broker, "2026-10-12")
    process(broker, "2026-10-12", "15:30")
    assert post.status is OrderStatus.FILLED and broker.get_position("A").quantity == 10
    assert broker.slippage_records[-1].quote.slippage_regime == "earnings_affected"


@pytest.mark.parametrize(
    "invalid",
    [
        {"fee_history_mode": "archive"},
        {"fee_snapshot_date": "2026-10-11"},
        {"slippage_mode": "summed"},
        {"slippage_missing_earnings": "ignore"},
        {"slippage_regular_bps": -1},
        {"slippage_early_close_bps": True},
        {"slippage_earnings_bps": float("nan")},
    ],
)
def test_cost_config_validation(invalid):
    with pytest.raises(ValueError):
        ConstraintConfig(**invalid)


@pytest.mark.parametrize(
    "changes",
    [
        {"slippage_type": SlippageType.SPREAD, "slippage_spread": 0.02},
        {"slippage_rate": 0.0001},
        {"stop_slippage_rate": 0.001},
        {"execution_price": ExecutionPrice.ASK},
    ],
)
def test_dynamic_model_refuses_implicit_double_cost_scenarios(changes):
    controller = ConstraintController(ConstraintConfig(), provider())
    with pytest.raises(ValueError, match="Regime slippage replaces"):
        broker_factory(controller, market)(cash_backtest_config(**changes))


def test_standalone_fallback_is_two_bps_and_unconfigured_upstream_preserved():
    standalone = Broker.from_config(cash_backtest_config())
    assert calculate_slippage(standalone.slippage_model, "A", 100, 100, 1000) == 0.02
    assert calculate_slippage(Broker().slippage_model, "A", 100, 100, 1000) == 0


def test_default_uniform_one_bps_and_earnings_ten_bps_cost_sensitivity():
    results = {}
    for name, settings in (
        ("default_2_3_5", ConstraintConfig()),
        (
            "uniform_1",
            ConstraintConfig(
                slippage_regular_bps=1, slippage_early_close_bps=1, slippage_earnings_bps=1
            ),
        ),
        ("earnings_10", ConstraintConfig(slippage_earnings_bps=10)),
    ):
        broker, _ = cost_broker((event(),), settings, cash=100000)
        tick(broker)
        broker.submit_order("A", 100)
        process(broker, "2026-10-09")
        broker.submit_order("A", -100)
        process(broker, "2026-10-09", "10:32")
        results[name] = broker.execution_cost_statistics()
    assert [results[n]["slippage"]["total_slippage"] for n in results] == pytest.approx([10, 2, 20])
    assert all(results[n]["fees"]["total_fees"] > 0 for n in results)
    assert ConstraintConfig().slippage_earnings_bps == 5


@pytest.mark.parametrize("api", ["plan", "weights", "configured_weights"])
@pytest.mark.parametrize("cycle", ["T+1", "T+2"])
@pytest.mark.parametrize(
    ("mode", "creation", "due1", "due2"),
    [
        ("last", "2026-10-30", "2026-11-02", "2026-11-03"),
        ("first", "2026-11-02", "2026-11-03", "2026-11-04"),
        ("semi_monthly", "2026-10-16", "2026-10-19", "2026-10-20"),
    ],
)
def test_costed_rotation_input_parity_waits_for_real_settlement(
    api, cycle, mode, creation, due1, due2
):
    settings = ConstraintConfig(
        settlement_cycle=cycle,
        rebalance_plan_enabled=True,
        rebalance_mode="semi_monthly" if mode == "semi_monthly" else "monthly",
        monthly_session="last" if mode == "last" else "first",
    )
    broker, _ = cost_broker(settings=settings)
    tick(broker, at("2026-10-01"))
    for asset in ASSETS[:4]:
        broker.submit_order(asset, 22)
    process(broker, "2026-10-01")
    targets = dict.fromkeys(("B", "C", "D", "E"), 0.22)
    tick(broker, at(creation))
    if api == "plan":
        plan = broker.create_rebalance_plan(targets, rebalance_id="rotation")
        assert plan is not None
        identifier = plan.plan_id
    else:
        broker.rebalance_to_weights(targets, rebalance_id="rotation" if api == "weights" else None)
        identifier = next(iter(broker.rebalance_plans))
    process(broker, creation)
    assert broker.get_position("A") is None and broker.get_position("E") is None
    assert broker.unsettled_cash == pytest.approx(
        22 * 99.98 - broker.fee_records[-1].fees.total_fees
    )
    process(broker, creation, "10:32")
    assert len(broker.fee_records) == 5 and broker.reserved_cash == 0
    if cycle == "T+2":
        process(broker, due1)
        assert broker.get_position("E") is None and len(broker.fee_records) == 5
    due = due1 if cycle == "T+1" else due2
    process(broker, due, "10:30")
    assert len(broker.fee_records) == 5 and broker.reserved_cash > 0
    process(broker, due)
    process(broker, due, "10:32")  # mark all holdings at the same observed reference price
    assert broker.rebalance_plans[identifier].status == "completed"
    assert set(broker.account.positions) == {"B", "C", "D", "E"}
    assert len(broker.fee_records) == len(broker.slippage_records) == 6
    stats = broker.execution_cost_statistics()
    equity = 10000 - stats["fees"]["total_fees"] - stats["slippage"]["total_slippage"]
    assert broker.get_account_value() == pytest.approx(equity)
    assert broker.get_position("E").quantity * 100 / equity == pytest.approx(0.22, abs=0.00005)
    assert broker.settled_cash >= 0.1 * equity and broker.reserved_cash == 0
    if mode == "last":
        assert broker.fee_model.volume("2026-10") == 110
        assert broker.fee_model.volume("2026-11") == pytest.approx(
            broker.get_position("E").quantity
        )


def test_expired_and_canceled_zero_fills_leave_all_cost_ledgers_empty():
    broker, _ = cost_broker()
    tick(broker)
    cancel = broker.submit_order("A", 20, order_type=OrderType.LIMIT, limit_price=99)
    assert broker.cancel_order(cancel.order_id)
    expire = broker.submit_order("B", 20, order_type=OrderType.LIMIT, limit_price=99)
    process(broker, "2026-10-09", "16:00")
    assert expire.status is OrderStatus.CANCELLED
    assert broker.fee_records == broker.slippage_records == ()
    assert broker.fee_model.monthly_volumes == {} and broker.cash == 10000
    assert broker.reserved_cash == 0


def test_announcement_after_half_day_close_shares_admission_and_slippage_mapping():
    release = event("2026-11-27", "13:05", "DURING")
    broker, controller = cost_broker((release,))
    _, affected, deadline = controller.earnings.sessions(release)
    assert affected.isoformat() == "2026-11-30"
    assert deadline.astimezone(at().tzinfo) == at("2026-11-27", "12:30")
    tick(broker, at("2026-11-30", "10:30"))
    order = broker.submit_order("A", 10)
    process(broker, "2026-11-30")
    assert order.status is OrderStatus.FILLED
    assert order.filled_price == pytest.approx(100.05)
    assert broker.slippage_records[-1].quote.slippage_regime == "earnings_affected"


@pytest.mark.parametrize("bps", [1, 2, 5])
def test_configured_percentage_sensitivity_preserves_actual_direction_and_cost_audit(bps):
    settings = ConstraintConfig(slippage_mode="configured")
    controller = ConstraintController(settings, provider())
    broker = broker_factory(controller, market)(
        cash_backtest_config(initial_cash=10000, slippage_rate=bps / 10000)
    )
    tick(broker)
    order = broker.submit_order("A", 20)
    process(broker, "2026-10-09")
    assert order.filled_price == pytest.approx(100 + bps / 100)
    broker.submit_order("A", -20)
    process(broker, "2026-10-09", "10:32")
    assert broker._execution_journal.fills[-1].price == pytest.approx(100 - bps / 100)
    assert all(r.quote.slippage_bps == pytest.approx(bps) for r in broker.slippage_records)
    assert all(r.quote.slippage_regime == "configured" for r in broker.slippage_records)
    assert broker.slippage_statistics()["total_slippage"] == pytest.approx(40 * bps / 100)


def test_project_strict_config_rejects_uncovered_history_without_mutating_ledgers():
    broker, _ = cost_broker(settings=ConstraintConfig(fee_history_mode="strict_historical"))
    tick(broker, at("2005-10-03"))
    with pytest.raises(ValueError, match="No explicit regulatory"):
        broker.submit_order("A", 20)
    assert broker.cash == 10000 and broker.reserved_cash == 0
    assert broker.fee_records == broker.slippage_records == () and broker._order_state.orders == []


def test_custom_commission_cost_summary_uses_canonical_fills_without_invented_breakdown():
    broker, _ = cost_broker(settings=ConstraintConfig(pricing_plan="custom"))
    tick(broker)
    broker.submit_order("A", 20)
    process(broker, "2026-10-09")
    stats = broker.execution_cost_statistics()
    assert stats["fees"]["total_fees"] == stats["fees"]["custom_unclassified_fees"] == 0.35
    assert stats["fee_scenarios"] == () and broker.fee_records == ()
    assert stats["slippage"]["total_slippage"] == pytest.approx(0.4)


def test_invalid_observed_target_reference_rejected():
    for price in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="reference_price"):
            replace(market(at(), "A", "submission"), reference_price=price)
