"""Corporate actions reconcile through the real account, orders, risk and Engine."""

import copy
from dataclasses import replace
from datetime import date
from types import MappingProxyType

import polars as pl
import pytest
from test_adapter import ASSETS, at, market, tick

from ml4t.backtest import Broker, DataFeed, OrderStatus, OrderType, Strategy
from ml4t.backtest.config import CommissionType, SlippageType
from ml4t.backtest.execution.limits import VolumeParticipationLimit
from ml4t.backtest.risk import StopLoss, TrailingStop
from ml4t.backtest.risk.position.composite import RuleChain
from ml4t.backtest.risk.position.dynamic import VolatilityStop, VolatilityTrailingStop
from ml4t.backtest.types import Position
from quant_constraints import (
    ConstraintConfig,
    ConstraintController,
    EarningsCoverage,
    EarningsEvent,
    InMemoryEarningsProvider,
    broker_factory,
    cash_backtest_config,
    constrained_engine,
)
from quant_constraints.corporate_actions import (
    CorporateAction,
    CorporateActionCoverage,
    CorporateActionProcessor,
    InMemoryCorporateActionProvider,
)
from quant_constraints.plans import RebalancePlan


def action(kind="SPLIT", event_id="event", day="2026-10-12", **changes):
    defaults = (
        {"split_ratio": 2.0}
        if kind == "SPLIT"
        else ({"dividend_per_share": 1.0} if kind == "CASH_DIVIDEND" else {})
    )
    defaults.update(changes)
    return CorporateAction(
        "stable-A",
        event_id,
        kind,
        date.fromisoformat(day),
        defaults.pop("available_at", at(day) if kind == "CASH_CREDIT" else at("2026-10-08")),
        "source-fixture",
        **defaults,
    )


def build(*events, settings=None, costs=False, earnings=(), **changes):
    settings = replace(
        settings or ConstraintConfig(),
        corporate_actions_enabled=True,
        pricing_plan="ibkr_pro_tiered" if costs else "custom",
        slippage_mode="regime" if costs else "configured",
        drift_reduction="disabled",
    )
    controller = ConstraintController(
        settings,
        InMemoryEarningsProvider(
            tuple(earnings),
            tuple(
                EarningsCoverage(a, at("1995-01-01"), at("2030-01-01"), "fixture") for a in ASSETS
            ),
        ),
    )
    provider = InMemoryCorporateActionProvider(
        tuple(events),
        tuple(
            CorporateActionCoverage(
                f"stable-{a}", at("1995-01-01"), date(1995, 1, 1), date(2030, 1, 1), "fixture"
            )
            for a in ASSETS
        ),
    )

    def context(ts, asset, phase):
        return replace(
            market(ts, asset, phase),
            security_id=f"stable-{asset}",
            execution_data_mode="raw_execution",
            risk_data_mode="raw_execution",
            signal_data_mode=settings.signal_data_mode,
        )

    defaults = (
        {}
        if costs
        else {
            "commission_type": CommissionType.NONE,
            "commission_per_share": 0.0,
            "commission_minimum": 0.0,
            "slippage_type": SlippageType.NONE,
            "slippage_rate": 0.0,
        }
    )
    defaults.update(changes)
    config = cash_backtest_config(initial_cash=10000, **defaults)
    broker = broker_factory(controller, context, corporate_action_provider=provider)(config)
    return broker, controller, provider, config, context


def held(broker, quantity=20.0, price=100.0):
    broker.account.cash = broker.account._lock_notional_free_cash = 10000 - quantity * price
    broker.positions["A"] = Position(
        "A",
        quantity,
        price,
        at(),
        current_price=price,
        initial_quantity=quantity,
        high_water_mark=price * 1.1,
        low_water_mark=price * 0.9,
        entry_commission=2.5,
        entry_slippage=0.1,
    )
    return broker.positions["A"]


@pytest.mark.parametrize("ratio", [2, 10, 0.1])
def test_split_preserves_all_accounting_and_excursions_without_trades(ratio):
    broker, _, _, _, _ = build(action(split_ratio=ratio))
    pos = held(broker, quantity=10.5)
    tick(broker)
    pos.max_favorable_excursion, pos.max_adverse_excursion = 0.1, -0.1
    before = (
        broker.cash,
        broker.settled_cash,
        broker.account.total_equity,
        pos.entry_commission,
        pos.entry_time,
        len(broker.fills),
        len(broker.trades),
    )
    tick(broker, at("2026-10-12"), price=100 / ratio)
    assert pos.quantity == pytest.approx(10.5 * ratio)
    assert pos.initial_quantity == pytest.approx(10.5 * ratio)
    assert (
        pos.entry_price,
        pos.current_price,
        pos.high_water_mark,
        pos.low_water_mark,
    ) == pytest.approx((100 / ratio, 100 / ratio, 110 / ratio, 90 / ratio))
    assert pos.entry_slippage == pytest.approx(0.1 / ratio)
    assert (pos.max_favorable_excursion, pos.max_adverse_excursion) == (0.1, -0.1)
    assert (
        broker.cash,
        broker.settled_cash,
        broker.account.total_equity,
        pos.entry_commission,
        pos.entry_time,
        len(broker.fills),
        len(broker.trades),
    ) == before
    assert broker.get_account_value() == pytest.approx(10000)
    assert broker.constraint_state().drawdown == 0


@pytest.mark.parametrize("rule", [StopLoss(0.05), TrailingStop(0.2)])
def test_raw_split_open_does_not_trigger_fake_stop(rule):
    broker, _, _, _, _ = build(action())
    held(broker)
    broker._risk_state.position_rules_by_asset["A"] = rule
    tick(broker)
    tick(broker, at("2026-10-12"), price=50)
    broker.evaluate_position_rules()
    broker._process_orders(use_open=True)
    assert not broker.fills
    assert broker.positions["A"].quantity == 40


@pytest.mark.parametrize("policy", ["adjust", "cancel"])
@pytest.mark.parametrize("ratio", [2, 10, 0.1])
def test_partial_bracket_split_keeps_live_oco_protection(policy, ratio):
    broker, controller, _, _, _ = build(
        action(split_ratio=ratio),
        settings=replace(
            ConstraintConfig(),
            split_order_policy=policy,
            buy_time_in_force="GTD",
            max_defer_sessions=5,
        ),
    )
    broker.execution_limits = VolumeParticipationLimit(0.001)
    tick(broker)
    parent, tp, stop = broker.submit_bracket("A", 20, take_profit=120, stop_loss=90)
    tick(broker, at(clock="10:31"))
    broker._process_orders(use_open=True)
    assert parent.filled_quantity == 10
    historical = copy.deepcopy(broker.fills)
    entries = list(broker.entry_times)
    tick(broker, at("2026-10-12"), price=100 / ratio)
    assert broker.positions["A"].quantity == pytest.approx(10 * ratio)
    assert parent.filled_quantity == pytest.approx(10 * ratio)
    assert tp.quantity == stop.quantity == pytest.approx(10 * ratio)
    assert (tp.limit_price, stop.stop_price) == pytest.approx((120 / ratio, 90 / ratio))
    assert tp.status is stop.status is OrderStatus.PENDING
    assert broker.fills == historical and broker.entry_times == entries
    if policy == "cancel":
        assert parent.status is OrderStatus.CANCELLED
        assert any(
            a.decision.code == "split_canceled_remainder_protection_retained"
            for a in controller.audit
        )
    else:
        assert broker._order_state.partial_quantities[parent.order_id] == pytest.approx(10 * ratio)
    # Genuine stop after conversion closes only actual exposure; OCO sibling cancels.
    broker.execution_limits = None
    tick(broker, at("2026-10-12", "10:31"), price=80 / ratio)
    broker._process_orders(use_open=True)
    assert "A" not in broker.positions
    assert stop.status is OrderStatus.FILLED and tp.status is OrderStatus.CANCELLED
    assert not broker.get_pending_orders()


@pytest.mark.parametrize(
    "order_type,prices",
    [
        (OrderType.LIMIT, {"limit_price": 99}),
        (OrderType.STOP, {"stop_price": 105}),
        (OrderType.STOP_LIMIT, {"stop_price": 105, "limit_price": 106}),
        (OrderType.TRAILING_STOP, {"trail_amount": 5}),
    ],
)
def test_split_adjusts_pending_order_prices_quantities_and_reservations(order_type, prices):
    broker, _, _, _, _ = build(
        action(),
        settings=replace(ConstraintConfig(), buy_time_in_force="GTD", max_defer_sessions=5),
    )
    tick(broker)
    order = broker.submit_order("A", 12, order_type=order_type, **prices)
    assert order.status is OrderStatus.PENDING
    reserve = order._reserved_cash
    tick(broker, at("2026-10-12"), price=50)
    assert order.quantity == 24
    assert order._reserved_cash == pytest.approx(reserve)
    for name, value in prices.items():
        assert getattr(order, name) == value / 2


def test_absolute_atr_pending_risk_and_plan_adjust_in_same_transaction():
    broker, _, _, _, _ = build(action())
    pos = held(broker)
    pos.context.update(atr=4.0, signal_price=100)
    rule = VolatilityStop(_entry_atr=4)
    broker._risk_state.position_rules = RuleChain([rule, VolatilityTrailingStop()])
    broker._risk_state.pending_exits["A"] = {"quantity": 10, "fill_price": 95}
    broker.plan_manager.records["p"] = RebalancePlan(
        "p",
        MappingProxyType({"A": 0.2}),
        at(),
        at("2026-10-15"),
        MappingProxyType({"A": 100}),
        status="waiting_cash",
    )
    tick(broker)
    tick(broker, at("2026-10-12"), price=50)
    assert pos.context["atr"] == 2 and pos.context["signal_price"] == 50
    assert broker._risk_state.position_rules_by_asset["A"].rules[0]._entry_atr == 2
    assert rule._entry_atr == 4  # another asset's global rule is not corrupted
    assert broker._risk_state.pending_exits["A"] == {"quantity": 20, "fill_price": 47.5}
    assert broker.rebalance_plans["p"].reference_prices["A"] == 50
    assert broker.rebalance_plans["p"].targets["A"] == 0.2


def test_dividend_ex_start_entitlement_equity_and_actual_credit_not_payable_date():
    dividend = action("CASH_DIVIDEND", payable_date=date(2026, 10, 13))
    credit = action(
        "CASH_CREDIT",
        "credit",
        "2026-10-14",
        parent_event_id="event",
        available_at=at("2026-10-14", "10:32"),
    )
    broker, _, _, _, _ = build(dividend, credit)
    held(broker, quantity=100)
    tick(broker)
    tick(broker, at("2026-10-12"), price=99)
    assert broker.cash == broker.settled_cash == broker.account.buying_power == 0
    assert broker.account._receivable_value == 100
    broker.account.mark_to_market({"A": 99})
    assert broker.account.total_equity == broker.get_account_value() == 10000
    assert broker.constraint_state().drawdown == 0
    tick(broker, at("2026-10-13"), price=99)
    assert broker.cash == 0  # payable metadata is not a credit confirmation
    tick(broker, at("2026-10-14"), price=99)
    assert broker.cash == 0  # future credit availability cannot leak
    tick(broker, at("2026-10-14", "10:32"), price=99)
    assert broker.cash == broker.settled_cash == 100
    assert broker.account._receivable_value == 0
    assert broker.get_account_value() == 10000
    assert not broker.fills and not broker.trades
    tick(broker, at("2026-10-14", "10:32"), price=99)
    assert broker.cash == 100  # repeated bar is exactly once


@pytest.mark.parametrize("rate,fee,actual", [(0, 0, None), (0.15, 0, None), (0.3, 0.01, 13.5)])
def test_tax_scenarios_and_actual_credit_reconciliation(rate, fee, actual):
    broker, _, _, _, _ = build(
        action("CASH_DIVIDEND", fee_per_share=fee),
        action("CASH_CREDIT", "credit", "2026-10-13", parent_event_id="event", credited_net=actual),
        settings=replace(
            ConstraintConfig(),
            dividend_withholding_rate=rate,
            dividend_tax_scenario="explicit_fixture_investor",
        ),
    )
    held(broker)
    tick(broker)
    tick(broker, at("2026-10-12"), price=99)
    expected = 20 * (1 - rate - fee)
    assert broker.account._receivable_value == pytest.approx(expected)
    assert broker.cash == 8000
    record = broker.corporate_action_evidence()["records"][0]
    assert (record["gross"], record["withholding"], record["fees"]) == pytest.approx(
        (20, 20 * rate, 20 * fee)
    )
    tick(broker, at("2026-10-13"), price=99)
    actual = expected if actual is None else actual
    assert broker.cash == pytest.approx(8000 + actual)
    assert broker.corporate_action_evidence()["income"] == pytest.approx(actual)


def test_ex_date_buys_no_entitlement_and_ex_date_sales_keep_receivable():
    event = action("CASH_DIVIDEND")
    buyer, _, _, _, _ = build(event)
    tick(buyer, at("2026-10-12"), price=99)
    order = buyer.submit_order("A", 20)
    tick(buyer, at("2026-10-12", "10:31"), price=99)
    buyer._process_orders(use_open=True)
    assert order.status is OrderStatus.FILLED and buyer.account._receivable_value == 0
    seller, _, _, _, _ = build(event)
    held(seller)
    tick(seller)
    tick(seller, at("2026-10-12"), price=99)
    sell = seller.submit_order("A", -20)
    tick(seller, at("2026-10-12", "10:31"), price=99)
    seller._process_orders(use_open=True)
    assert sell.status is OrderStatus.FILLED and seller.account._receivable_value == 20


def test_latest_pit_revision_duplicates_metadata_revision_and_restore():
    initial = action(split_ratio=2)
    revised = replace(initial, split_ratio=10, version=2, available_at=at("2026-10-09"))
    broker, _, provider, _, _ = build(initial, revised)
    held(broker)
    tick(broker)
    snapshot = broker._snapshot_lifecycle_state(
        all_positions=True, all_pending_orders=True, risk_rules=True, all_asset_stats=True
    )
    tick(broker, at("2026-10-12"), price=10)
    assert broker.positions["A"].quantity == 200
    broker.corporate_action_processor = CorporateActionProcessor(broker, provider)
    tick(broker, at("2026-10-12"), price=10)
    assert broker.positions["A"].quantity == 200
    broker._restore_lifecycle_state(snapshot)
    tick(broker, at("2026-10-12"), price=10)
    assert broker.positions["A"].quantity == 200
    provider.events += (
        replace(
            revised, source="metadata-revision", version=3, available_at=at("2026-10-12", "10:31")
        ),
    )
    tick(broker, at("2026-10-12", "10:31"), price=10)
    assert len(broker.corporate_action_evidence()["records"]) == 1


@pytest.mark.parametrize("change", [{"split_ratio": 3}, {"cancelled": True}])
def test_applied_financial_revision_or_cancellation_fails_without_mutation(change):
    original = action()
    broker, _, provider, _, _ = build(original)
    held(broker)
    tick(broker)
    tick(broker, at("2026-10-12"), price=50)
    before = copy.deepcopy(
        broker.account.__dict__, {id(broker.account.policy): broker.account.policy}
    )
    provider.events += (
        replace(original, version=2, available_at=at("2026-10-12", "10:31"), **change),
    )
    with pytest.raises(ValueError, match="revised"):
        tick(broker, at("2026-10-12", "10:31"), price=50)
    assert broker.account.__dict__ == before


def test_missing_effective_bar_recovers_known_event_and_scales_stale_mark():
    broker, _, _, _, _ = build(
        action(), settings=replace(ConstraintConfig(), missing_price="defer")
    )
    held(broker)
    tick(broker)
    broker._update_time(
        at("2026-10-13"), {"B": 100}, {"B": 100}, {"B": 100}, {"B": 100}, {"B": 1000}, {}
    )
    assert broker.positions["A"].quantity == 40
    assert broker.get_last_price("A") == 50
    assert broker.get_account_value() == 10000
    record = broker.corporate_action_evidence()["records"][0]
    assert record["recovered_after_gap"] and record["missing_asset_bar"]


@pytest.mark.parametrize(
    "problem",
    [
        "late",
        "coverage",
        "retrospective",
        "identity",
        "execution",
        "signal",
        "custom_rule",
        "precision",
    ],
)
def test_fail_closed_preflight_or_atomic_transform_preserves_cash_positions_orders_and_time(
    problem,
):
    event = action(available_at=at("2026-10-12", "10:31")) if problem == "late" else action()
    broker, _, provider, _, context = build(event)
    held(broker, quantity=10.5 if problem == "precision" else 20)
    tick(broker)
    if problem == "late":
        tick(broker, at("2026-10-12"), price=50)
    elif problem == "coverage":
        provider.coverage = ()
    elif problem == "retrospective":
        provider.coverage = tuple(replace(c, point_in_time=False) for c in provider.coverage)
    elif problem in {"identity", "execution", "signal"}:
        override = (
            {"security_id": "reused-ticker"}
            if problem == "identity"
            else (
                {"execution_data_mode": "total_return_signal"}
                if problem == "execution"
                else {"signal_data_mode": "raw_execution"}
            )
        )
        broker.context_provider = lambda ts, asset, phase: replace(
            context(ts, asset, phase), **override
        )
    elif problem == "custom_rule":
        broker._risk_state.position_rules_by_asset["A"] = object()
    elif problem == "precision":
        provider.events = (replace(event, split_ratio=0.1, quantity_precision=0),)
    before = copy.deepcopy(
        broker.account.__dict__, {id(broker.account.policy): broker.account.policy}
    )
    old_time = broker._market_state.time
    with pytest.raises(ValueError):
        tick(broker, at("2026-10-12", "10:31"), price=50)
    assert broker.account.__dict__ == before
    assert broker._market_state.time == old_time
    assert broker.corporate_action_processor.failures


@pytest.mark.parametrize(
    "kind,changes",
    [
        ("MERGER", {}),
        ("SPINOFF", {}),
        ("DELISTING", {}),
        ("SYMBOL_CHANGE", {}),
        ("SPLIT", {"fractional_policy": "cash_in_lieu"}),
        ("CASH_DIVIDEND", {"terms": "due_bill"}),
        ("CASH_DIVIDEND", {"currency": "EUR"}),
        ("CASH_DIVIDEND", {"dividend_basis": "pre_split"}),
    ],
)
def test_unsupported_exposed_actions_do_not_erase_holdings_or_create_cash(kind, changes):
    broker, _, _, _, _ = build(action(kind, **changes))
    held(broker)
    tick(broker)
    with pytest.raises(ValueError):
        tick(broker, at("2026-10-12"), price=50)
    assert broker.positions["A"].quantity == 20 and broker.cash == 8000


@pytest.mark.parametrize("mode", ["raw_execution", "split_adjusted_signal", "total_return_signal"])
@pytest.mark.parametrize("credit", [False, True])
def test_engine_public_equity_income_trades_and_persistence_reconcile(tmp_path, mode, credit):
    events = [action(), action("CASH_DIVIDEND", "dividend", dividend_per_share=0.5)]
    if credit:
        events.append(action("CASH_CREDIT", "credit", "2026-10-13", parent_event_id="dividend"))
    broker, ctrl, provider, config, context = build(
        *events, settings=replace(ConstraintConfig(), signal_data_mode=mode)
    )
    stamps = [at(), at(clock="10:31"), at("2026-10-12"), at("2026-10-13")]
    feed = DataFeed(
        prices_df=pl.DataFrame(
            [
                {
                    "timestamp": ts,
                    "asset": "A",
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 10000.0,
                }
                for ts, price in zip(stamps, [100.0, 100.0, 49.5, 49.5], strict=True)
            ]
        )
    )

    class FixtureIntent(Strategy):
        def on_data(self, timestamp, data, signals, broker):
            if timestamp == stamps[0]:
                broker.submit_order("A", 20)

    result = constrained_engine(
        feed, FixtureIntent(), ctrl, context, config, corporate_action_provider=provider
    ).run()
    assert [value for _, value in result.equity_curve] == [10000] * 4
    evidence = result.metrics["corporate_actions_v1"]
    assert evidence["income"] == 20
    assert evidence["outstanding"] == (0 if credit else 20)
    assert len(result.fills) == 1 and result.fills[0].quantity == 20
    assert result.trades[0].quantity == 40 and result.trades[0].entry_price == 50
    assert result.trades[0].pnl == -20  # income separate from trading P&L
    assert result.metrics["total_commission"] == 0
    assert result.metrics["total_filled_notional"] == 2000
    result.to_parquet(tmp_path / "result")
    restored = type(result).from_parquet(tmp_path / "result")
    assert restored.metrics["corporate_actions_v1"] == evidence


def test_unconfigured_native_broker_and_constraints_remain_opt_in():
    from test_adapter import make

    native = Broker.from_config(cash_backtest_config())
    tick(native)
    tick(native, at("2026-10-12"), price=50)
    assert not hasattr(native.account, "_corporate_action_state")
    constrained, _ = make()
    tick(constrained)
    assert constrained.corporate_action_evidence() is None
    with pytest.raises(ValueError, match="both opt-in"):
        broker_factory(
            constrained.controller,
            market,
            corporate_action_provider=InMemoryCorporateActionProvider(),
        )(cash_backtest_config())


@pytest.mark.parametrize("ratio", [0, -1, float("nan"), True])
def test_invalid_split_ratio_rejected(ratio):
    with pytest.raises(ValueError):
        action(split_ratio=ratio)


@pytest.mark.parametrize(
    "changes",
    [
        {"corporate_actions_enabled": 1},
        {"split_order_policy": "guess"},
        {"execution_data_mode": "total_return_signal"},
        {"signal_data_mode": "unknown"},
        {"dividend_withholding_rate": 1.1},
        {"dividend_withholding_rate": 0.3},
        {"dividend_tax_scenario": ""},
    ],
)
def test_config_rejects_ambiguous_data_orders_and_tax(changes):
    with pytest.raises(ValueError):
        ConstraintConfig(**changes)


@pytest.mark.parametrize("cycle", ["T+1", "T+2"])
@pytest.mark.parametrize(
    "split_day,bps,earnings",
    [("2026-10-12", 2, False), ("2026-11-27", 3, False), ("2026-10-12", 5, True)],
)
def test_actions_preserve_full_costs_fee_volume_and_actual_sale_settlement(
    cycle, split_day, bps, earnings
):
    prior = "2026-11-25" if split_day == "2026-11-27" else "2026-10-07"
    event = EarningsEvent("A", "earn", at(split_day, "08:00"), at(prior), "BMO", "fixture")
    broker, _, _, _, _ = build(
        action(day=split_day),
        action("CASH_DIVIDEND", "dividend", split_day, dividend_per_share=0.5),
        settings=replace(ConstraintConfig(), hold_through_earnings=True, settlement_cycle=cycle),
        costs=True,
        earnings=(event,) if earnings else (),
    )
    tick(broker, at(prior))
    order = broker.submit_order("A", 20)
    tick(broker, at(prior, "10:31"))
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.FILLED
    fees = tuple(broker.fee_records)
    volumes = dict(broker.fee_model.monthly_volumes)
    generation = dict(broker.fee_bridge.generations)
    commission = broker.positions["A"].entry_commission
    tick(broker, at(split_day), price=49.5)
    assert broker.positions["A"].quantity == 40
    assert broker.positions["A"].entry_commission == commission
    assert broker.account._receivable_value == 20
    assert tuple(broker.fee_records) == fees and dict(broker.fee_model.monthly_volumes) == volumes
    assert broker.fee_bridge.generations == generation
    cash = broker.cash
    sell = broker.submit_order("A", -40)
    tick(broker, at(split_day, "10:31"), price=49.5)
    broker._process_orders(use_open=True)
    assert sell.status is OrderStatus.FILLED
    assert broker.slippage_records[-1].quote.slippage_bps == bps
    assert broker.fee_model.monthly_volumes[split_day[:7]] == 60
    proceeds = broker.cash - cash
    assert broker.unsettled_cash == pytest.approx(proceeds)
    assert broker.settled_cash == pytest.approx(cash)
    from quant_constraints.calendar import settlement_date

    due = settlement_date(date.fromisoformat(split_day), cycle)
    tick(broker, at(due.isoformat()), price=49.5)
    assert broker.settled_cash == broker.cash
    assert broker.account._receivable_value == 20  # not made tradable by sale settlement


@pytest.mark.parametrize("mode,day", [("monthly", "2026-11-02"), ("semi_monthly", "2026-11-16")])
@pytest.mark.parametrize("input_style", ["legacy", "keyword", "quote_aware"])
def test_action_schedule_and_target_units_consistent_in_all_bar_inputs(mode, day, input_style):
    broker, controller, _, _, _ = build(
        action(day=day), settings=replace(ConstraintConfig(), rebalance_mode=mode)
    )
    held(broker)
    tick(broker)
    marks, volumes = dict.fromkeys(ASSETS, 50), dict.fromkeys(ASSETS, 10000)
    if input_style == "legacy":
        broker._update_time(at(day), marks, marks, marks, marks, volumes, {})
    elif input_style == "keyword":
        broker._update_time(
            at(day), marks, marks, highs=marks, lows=marks, volumes=volumes, signals={}
        )
    else:
        broker._update_time(
            at(day), marks, marks, marks, marks, marks, volumes, {}, {}, {}, {}, {}, {}, {}
        )
    assert broker.positions["A"].quantity == 40
    assert broker.constraint_state().equity == 10000
    assert controller.rebalance.scheduled(broker.constraint_state(), at(day))
    assert (
        broker.order_target_percent("A", 0.15).quantity == 10
    )  # reduction at raw post-split price


def test_credit_confirmation_on_nontrading_date_recovered_without_double_cash():
    broker, _, _, _, _ = build(
        action("CASH_DIVIDEND", payable_date=date(2026, 10, 17)),
        action("CASH_CREDIT", "credit", "2026-10-17", parent_event_id="event"),
    )
    held(broker)
    tick(broker)
    tick(broker, at("2026-10-12"), price=99)
    tick(broker, at("2026-10-19"), price=99)
    assert broker.cash == 8020 and broker.account._receivable_value == 0
    assert broker.corporate_action_evidence()["records"][-1]["effective_date"] == "2026-10-17"


def test_second_credit_id_cannot_spend_same_entitlement_twice():
    broker, _, provider, _, _ = build(
        action("CASH_DIVIDEND"),
        action("CASH_CREDIT", "credit", "2026-10-13", parent_event_id="event"),
    )
    held(broker)
    tick(broker)
    tick(broker, at("2026-10-12"), price=99)
    tick(broker, at("2026-10-13"), price=99)
    provider.events += (action("CASH_CREDIT", "duplicate", "2026-10-14", parent_event_id="event"),)
    with pytest.raises(ValueError, match="already credited"):
        tick(broker, at("2026-10-14"), price=99)
    assert broker.cash == 8020


def test_unpaid_dividend_not_reserved_and_credited_cash_cannot_be_used_twice():
    broker, _, _, _, _ = build(
        action("CASH_DIVIDEND", dividend_per_share=10),
        action("CASH_CREDIT", "credit", "2026-10-13", parent_event_id="event"),
        settings=replace(ConstraintConfig(), cash_reserve=0),
    )
    held(broker, quantity=100)
    tick(broker)
    tick(broker, at("2026-10-12"), price=90)
    assert broker.submit_order("B", 1000 / 90).rejection_code == "settled_cash"
    tick(broker, at("2026-10-13"), price=90)
    first = broker.submit_order("B", 1000 / 90)
    assert first.status is OrderStatus.PENDING
    assert broker.reserved_cash == pytest.approx(1000)
    assert broker.submit_order("C", 1000 / 90).rejection_code == "settled_cash"
    tick(broker, at("2026-10-13", "10:31"), price=90)
    broker._process_orders(use_open=True)
    assert first.status is OrderStatus.FILLED
    assert broker.cash == pytest.approx(0)


def test_terminal_filled_bracket_parent_restates_only_live_units_not_historical_fills():
    broker, _, _, _, _ = build(action())
    tick(broker)
    parent, tp, stop = broker.submit_bracket("A", 20, take_profit=120, stop_loss=90)
    tick(broker, at(clock="10:31"))
    broker._process_orders(use_open=True)
    assert parent.status is OrderStatus.FILLED
    fill = copy.deepcopy(broker.fills[0])
    tick(broker, at("2026-10-12"), price=50)
    assert parent.filled_quantity == tp.quantity == stop.quantity == 40
    assert broker.fills[0] == fill and fill.quantity == 20
    tick(broker, at("2026-10-12", "10:31"), price=40)
    broker._process_orders(use_open=True)
    assert "A" not in broker.positions and stop.status is OrderStatus.FILLED
    assert tp.status is OrderStatus.CANCELLED and len(broker.trades) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"security_id": ""},
        {"version": 0},
        {"kind": ""},
        {"source": ""},
        {"withholding_rate": 1.1},
        {"quantity_precision": True},
        {"quantity_precision": 13},
        {"cancelled": 1},
        {"effective_date": "2026-10-12"},
        {"credited_net": -1},
    ],
)
def test_event_validation_before_any_booking(changes):
    with pytest.raises(ValueError):
        replace(action(), **changes)


def test_invalid_dividend_credit_and_coverage_rejected():
    with pytest.raises(ValueError):
        action("CASH_DIVIDEND", dividend_per_share=None)
    with pytest.raises(ValueError):
        action("CASH_CREDIT")
    with pytest.raises(ValueError, match="precede"):
        action("CASH_CREDIT", parent_event_id="event", available_at=at("2026-10-08"))
    coverage = build()[2].coverage[0]
    with pytest.raises(ValueError):
        replace(coverage, source="")
    with pytest.raises(ValueError):
        replace(coverage, missing=1)
    conflicting = replace(action(), split_ratio=3)
    provider = InMemoryCorporateActionProvider((action(), conflicting))
    with pytest.raises(ValueError, match="Conflicting"):
        provider.snapshot("stable-A", at())


def test_cancelled_visible_event_does_not_apply_and_different_security_does_not_leak():
    event = action(cancelled=True)
    broker, _, provider, _, _ = build(event)
    held(broker)
    provider.events += (replace(action(event_id="other"), security_id="unrelated"),)
    tick(broker)
    tick(broker, at("2026-10-12"))
    assert broker.positions["A"].quantity == 20
    assert not broker.corporate_action_evidence()["records"]


def test_late_dividend_after_ex_date_sale_cannot_silently_lose_entitlement():
    broker, _, _, _, _ = build(action("CASH_DIVIDEND", available_at=at("2026-10-12", "10:32")))
    held(broker)
    tick(broker)
    tick(broker, at("2026-10-12"), price=99)
    broker.submit_order("A", -20)
    tick(broker, at("2026-10-12", "10:31"), price=99)
    broker._process_orders(use_open=True)
    assert "A" not in broker.positions
    cash = broker.cash
    with pytest.raises(ValueError, match="Late"):
        tick(broker, at("2026-10-12", "10:32"), price=99)
    assert broker.cash == cash


def test_credit_cannot_create_income_for_zero_share_entitlement():
    broker, _, _, _, _ = build(
        action("CASH_DIVIDEND"),
        action("CASH_CREDIT", "credit", "2026-10-13", parent_event_id="event", credited_net=20),
    )
    tick(broker, at("2026-10-12"), price=99)
    with pytest.raises(ValueError, match="exceeds eligible"):
        tick(broker, at("2026-10-13"), price=99)
    assert broker.cash == 10000


def test_cancel_split_policy_closes_rebalance_authorization_without_recreating_order():
    broker, _, _, _, _ = build(
        action(),
        settings=replace(
            ConstraintConfig(),
            split_order_policy="cancel",
            buy_time_in_force="GTD",
            max_defer_sessions=5,
        ),
    )
    tick(broker)
    order = broker.submit_order("A", 12, order_type=OrderType.LIMIT, limit_price=99)
    order.rebalance_id = "p"
    broker.plan_manager.records["p"] = RebalancePlan(
        "p",
        MappingProxyType({"A": 0.2}),
        at(),
        at("2026-10-15"),
        MappingProxyType({"A": 100}),
        status="buying",
        order_ids=(order.order_id,),
    )
    tick(broker, at("2026-10-12"), price=50)
    assert broker.rebalance_plans["p"].status == "canceled"
    assert order.status is OrderStatus.CANCELLED
    broker._process_orders(use_open=True)
    assert not broker.get_pending_orders() and not broker.fills


def test_canonical_preopen_intents_fail_explicitly_instead_of_retaining_stale_share_targets():
    from types import SimpleNamespace

    broker, _, _, _, _ = build(action())
    tick(broker)
    broker._preopen_target_manager = SimpleNamespace(target_count=1)
    with pytest.raises(ValueError, match="pre-open intents"):
        tick(broker, at("2026-10-12"), price=50)
    assert broker.cash == 10000 and not broker.fills
