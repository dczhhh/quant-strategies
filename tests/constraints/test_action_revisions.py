"""Historical zero-exposure revisions and credit of an old dividend entitlement."""

import copy
from dataclasses import replace
from datetime import date
from types import MappingProxyType

import polars as pl
import pytest
from test_adapter import at, tick
from test_corporate_actions import action, build, held

from ml4t.backtest import DataFeed, OrderStatus, OrderType, Strategy
from ml4t.backtest.types import OrderSide
from quant_constraints import ConstraintConfig, constrained_engine
from quant_constraints.corporate_actions import CorporateActionProcessor
from quant_constraints.plans import RebalancePlan


def position_economics(broker):
    # Time advancing normally increments bars_held; the event must not change
    # quantities, basis, prices, excursions, fees or risk/quote context.
    return {
        asset: {name: value for name, value in vars(pos).items() if name != "bars_held"}
        for asset, pos in broker.positions.items()
    }


@pytest.mark.parametrize("path", ["flat", "rebuy_same_day", "round_trips"])
@pytest.mark.parametrize("costs", [False, True])
def test_engine_old_credit_survives_flat_and_new_positions(tmp_path, path, costs):
    dividend = action("CASH_DIVIDEND", withholding_rate=0.15, fee_per_share=0.05)
    credit = action(
        "CASH_CREDIT",
        "credit",
        "2026-10-14",
        parent_event_id="event",
        credited_net=15.5,
        available_at=at("2026-10-14", "13:01"),
    )
    _, controller, provider, config, context = build(dividend, credit, costs=costs)
    stamps = [
        at(),
        at(clock="10:31"),
        at("2026-10-12"),
        at("2026-10-13"),
        at("2026-10-13", "10:31"),
    ]
    intents = {stamps[0]: 20, stamps[3]: -20}
    if path == "round_trips":
        stamps += [at("2026-10-13", f"10:{minute}") for minute in range(32, 36)]
        intents.update({stamps[-4]: 12, stamps[-2]: -12})
    stamps += [at("2026-10-14"), at("2026-10-14", "10:31")]
    if path != "flat":
        intents[stamps[-2]] = 15
    stamps += [at("2026-10-14", clock) for clock in ("13:00", "13:01", "13:02")]
    prices = [100.0 if ts.date() == date(2026, 10, 9) else 99.0 for ts in stamps]
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
                for ts, price in zip(stamps, prices, strict=True)
            ]
        )
    )
    observations = {}

    class ExternalIntents(Strategy):
        def on_data(self, timestamp, data, signals, broker):
            if timestamp in intents:
                assert broker.submit_order("A", intents[timestamp]).status is OrderStatus.PENDING
            if timestamp.date() == date(2026, 10, 14) and timestamp.hour == 13:
                observations[timestamp.minute] = (
                    broker.cash,
                    broker.settled_cash,
                    broker.account._receivable_value,
                    broker.get_account_value(),
                    len(broker.fills),
                    len(broker.fee_records),
                )

    engine = constrained_engine(
        feed, ExternalIntents(), controller, context, config, corporate_action_provider=provider
    )
    result = engine.run()  # root fixture also checks all accounting invariants
    before, after = observations[0], observations[1]
    assert after[0] - before[0] == pytest.approx(15.5)
    assert after[1] - before[1] == pytest.approx(15.5)
    assert before[2] == 16 and after[2] == 0
    assert after[3] - before[3] == pytest.approx(-0.5)
    assert after[4:] == before[4:]
    assert observations[2] == after
    quantities = [20, -20] + ([12, -12] if path == "round_trips" else [])
    quantities += [15] if path != "flat" else []
    assert [
        fill.quantity if fill.side is OrderSide.BUY else -fill.quantity for fill in result.fills
    ] == quantities
    evidence = result.metrics["corporate_actions_v1"]
    ex, payment = evidence["records"]
    assert ex["eligible_quantity"] == payment["eligible_quantity"] == 20
    assert (ex["gross"], ex["withholding"], ex["fees"]) == (20, 3, 1)
    assert payment["income_delta"] == -0.5
    assert evidence["income"] == 15.5 and evidence["outstanding"] == 0
    result.to_parquet(tmp_path / "result")
    assert (
        type(result).from_parquet(tmp_path / "result").metrics["corporate_actions_v1"] == evidence
    )
    broker = engine.broker
    before_cash, fills = broker.cash, copy.deepcopy(broker.fills)
    tick(broker, at("2026-10-14", "13:02"), price=99)
    assert broker.cash == before_cash and broker.fills == fills
    provider.events += (
        replace(credit, event_id="second-credit", available_at=at("2026-10-14", "13:03")),
    )
    with pytest.raises(ValueError, match="already credited"):
        tick(broker, at("2026-10-14", "13:03"), price=99)
    assert broker.cash == before_cash and broker.fills == fills


@pytest.mark.parametrize("kind", ["SPLIT", "CASH_DIVIDEND", "MERGER"])
@pytest.mark.parametrize("later_exposure", ["flat", "position", "order", "protection"])
def test_observed_only_revision_never_replays_on_later_exposure(kind, later_exposure):
    event = action(kind)
    broker, _, provider, _, _ = build(
        event, settings=replace(ConstraintConfig(), buy_time_in_force="GTD", max_defer_sessions=5)
    )
    tick(broker)
    tick(broker, at("2026-10-12"))
    assert broker.corporate_action_evidence()["records"][0]["scope"] == "observed_only"
    tick(broker, at("2026-10-13"))
    if later_exposure == "position":
        order = broker.submit_order("A", 12)
        tick(broker, at("2026-10-13", "10:31"))
        broker._process_orders(use_open=True)
        assert order.status is OrderStatus.FILLED
    elif later_exposure == "order":
        assert (
            broker.submit_order("A", 12, order_type=OrderType.LIMIT, limit_price=90).status
            is OrderStatus.PENDING
        )
    elif later_exposure == "protection":
        parent, _, _ = broker.submit_bracket("A", 12, take_profit=120, stop_loss=90)
        assert parent.status is OrderStatus.PENDING
    change = {"split_ratio": 3} if kind == "SPLIT" else {"terms": "revised-unheld-terms"}
    revised = replace(event, version=2, available_at=at("2026-10-14"), **change)
    provider.events += (revised,)
    before = (
        broker.cash,
        broker.settled_cash,
        broker.get_account_value(),
        copy.deepcopy(position_economics(broker)),
        copy.deepcopy([vars(order) for order in broker.orders]),
        copy.deepcopy(broker.fills),
        dict(broker.account._receivables),
    )
    snapshot = broker._snapshot_lifecycle_state(
        all_positions=True, all_pending_orders=True, risk_rules=True, all_asset_stats=True
    )
    for replay in range(3):
        if replay == 1:
            broker.corporate_action_processor = CorporateActionProcessor(broker, provider)
        if replay == 2:
            broker._restore_lifecycle_state(snapshot)
        tick(broker, at("2026-10-14"))
        assert (
            broker.cash,
            broker.settled_cash,
            broker.get_account_value(),
            position_economics(broker),
            [vars(order) for order in broker.orders],
            broker.fills,
            broker.account._receivables,
        ) == before
        evidence = broker.corporate_action_evidence()
        assert len(evidence["records"]) == len(evidence["observation_revisions"]) == 1
        assert evidence["income"] == 0
        assert evidence["observation_revisions"][0]["terms"]["version"] == 2
        assert broker.corporate_action_processor.state["processed"][event.key] == revised


@pytest.mark.parametrize("change", [{"cancelled": True}, {"split_ratio": 3}])
def test_unheld_split_revision_and_cancellation_are_observations(change):
    event = action()
    broker, _, provider, _, _ = build(event)
    tick(broker, at("2026-10-12"), price=50)
    provider.events += (replace(event, version=2, available_at=at("2026-10-13"), **change),)
    tick(broker, at("2026-10-13"), price=50)
    tick(broker, at("2026-10-13"), price=50)
    assert broker.cash == broker.get_account_value() == 10000
    assert not broker.positions and not broker.orders and not broker.fills
    assert len(broker.corporate_action_evidence()["observation_revisions"]) == 1


@pytest.mark.parametrize("change", [{"effective_date": date(2026, 10, 14)}, {"kind": "MERGER"}])
def test_observed_revision_cannot_reuse_proof_for_a_different_date_or_kind(change):
    event = action()
    broker, _, provider, _, _ = build(event)
    tick(broker, at("2026-10-12"))
    provider.events += (replace(event, version=2, available_at=at("2026-10-13"), **change),)
    with pytest.raises(ValueError, match="scope revised"):
        tick(broker, at("2026-10-13"))
    assert broker.cash == 10000 and not broker.corporate_action_evidence()["observation_revisions"]


@pytest.mark.parametrize("kind", ["SPLIT", "CASH_DIVIDEND"])
def test_applied_revision_stays_strict_after_position_closed(kind):
    event = action(kind)
    broker, _, provider, _, _ = build(event)
    held(broker)
    tick(broker)
    tick(broker, at("2026-10-12"), price=50 if kind == "SPLIT" else 99)
    sell = broker.submit_order("A", -broker.positions["A"].quantity)
    tick(broker, at("2026-10-12", "10:31"), price=50 if kind == "SPLIT" else 99)
    broker._process_orders(use_open=True)
    assert sell.status is OrderStatus.FILLED and not broker.positions
    provider.events += (replace(event, version=2, available_at=at("2026-10-13"), cancelled=True),)
    before = copy.deepcopy(broker.corporate_action_processor.state)
    cash = broker.cash
    with pytest.raises(ValueError, match="Applied corporate action revised"):
        tick(broker, at("2026-10-13"))
    assert broker.cash == cash and broker.corporate_action_processor.state == before
    assert before["scopes"][event.key] == "economically_applied"
    assert broker.account._receivable_value == (20 if kind == "CASH_DIVIDEND" else 0)


@pytest.mark.parametrize("exposure", ["order", "protection", "plan", "pending_exit"])
def test_split_exposure_proof_includes_orders_protection_plans_and_queued_risk(exposure):
    event = action()
    broker, _, provider, _, _ = build(
        event, settings=replace(ConstraintConfig(), buy_time_in_force="GTD", max_defer_sessions=5)
    )
    tick(broker)
    if exposure == "order":
        broker.submit_order("A", 12, order_type=OrderType.LIMIT, limit_price=90)
    elif exposure == "protection":
        broker.submit_bracket("A", 12, take_profit=120, stop_loss=90)
    elif exposure == "plan":
        broker.plan_manager.records["p"] = RebalancePlan(
            "p",
            MappingProxyType({"A": 0.2}),
            at(),
            at("2026-10-15"),
            MappingProxyType({"A": 100}),
            status="waiting_cash",
        )
    else:
        broker._risk_state.pending_exits["A"] = {"quantity": 12, "fill_price": 90}
    tick(broker, at("2026-10-12"), price=50)
    assert broker.corporate_action_evidence()["records"][0]["scope"] == "economically_applied"
    provider.events += (
        replace(event, version=2, available_at=at("2026-10-12", "10:31"), split_ratio=3),
    )
    with pytest.raises(ValueError, match="Applied corporate action revised"):
        tick(broker, at("2026-10-12", "10:31"), price=50)


@pytest.mark.parametrize("restore", [False, True])
def test_legacy_checkpoint_without_scope_proof_is_conservative(restore):
    event = action()
    broker, _, provider, _, _ = build(event)
    tick(broker, at("2026-10-12"))
    del broker.corporate_action_processor.state["scopes"]
    del broker.corporate_action_processor.state["revisions"]
    snapshot = broker._snapshot_lifecycle_state(
        all_positions=True, all_pending_orders=True, risk_rules=True, all_asset_stats=True
    )
    broker.corporate_action_processor = CorporateActionProcessor(broker, provider)
    if restore:
        broker._restore_lifecycle_state(snapshot)
    assert broker.corporate_action_evidence()["observation_revisions"] == []
    provider.events += (replace(event, version=2, available_at=at("2026-10-13"), split_ratio=3),)
    with pytest.raises(ValueError, match="Applied corporate action revised"):
        tick(broker, at("2026-10-13"))


def test_credit_uses_old_checkpoint_quantity_when_entitlement_predates_quantity_field():
    broker, _, _, _, _ = build(
        action("CASH_DIVIDEND"),
        action("CASH_CREDIT", "credit", "2026-10-14", parent_event_id="event"),
    )
    held(broker)
    tick(broker)
    tick(broker, at("2026-10-12"), price=99)
    del broker.corporate_action_processor.state["entitlements"][("stable-A", "event")]["quantity"]
    broker.positions.clear()
    tick(broker, at("2026-10-14"), price=99)
    assert broker.corporate_action_evidence()["records"][-1]["eligible_quantity"] == 20


def test_observation_revision_rolls_back_with_failed_credit_in_same_bar():
    event = action()
    broker, _, provider, _, _ = build(event)
    tick(broker, at("2026-10-12"))
    provider.events += (
        replace(event, version=2, split_ratio=3, available_at=at("2026-10-13")),
        action("CASH_CREDIT", "bad-credit", "2026-10-13", parent_event_id="unknown"),
    )
    before = copy.deepcopy(broker.corporate_action_processor.state)
    with pytest.raises(ValueError, match="Unknown/already credited"):
        tick(broker, at("2026-10-13"))
    assert broker.corporate_action_processor.state == before
    assert broker.cash == broker.get_account_value() == 10000


def test_observed_metadata_revision_updates_latest_version_once():
    event = action()
    revised = replace(event, version=2, source="metadata-revision", available_at=at("2026-10-13"))
    broker, _, _, _, _ = build(event, revised)
    tick(broker, at("2026-10-12"))
    tick(broker, at("2026-10-13"))
    tick(broker, at("2026-10-13"))
    assert broker.corporate_action_processor.state["processed"][event.key] == revised
    assert len(broker.corporate_action_evidence()["observation_revisions"]) == 1


def test_engine_observed_revision_preserves_new_basis_and_result_evidence(tmp_path):
    event = action()
    revised = replace(event, version=2, split_ratio=3, available_at=at("2026-10-14"))
    _, controller, provider, config, context = build(event, revised)
    stamps = [at("2026-10-12"), at("2026-10-13"), at("2026-10-13", "10:31"), at("2026-10-14")]
    feed = DataFeed(
        prices_df=pl.DataFrame(
            [
                {
                    "timestamp": ts,
                    "asset": "A",
                    "open": 100.0,
                    "high": 100.0,
                    "low": 100.0,
                    "close": 100.0,
                    "volume": 10000.0,
                }
                for ts in stamps
            ]
        )
    )

    class ExternalIntent(Strategy):
        def on_data(self, timestamp, data, signals, broker):
            if timestamp == stamps[1]:
                assert broker.submit_order("A", 12).status is OrderStatus.PENDING

    result = constrained_engine(
        feed, ExternalIntent(), controller, context, config, corporate_action_provider=provider
    ).run()
    assert [value for _, value in result.equity_curve] == [10000] * 4
    assert len(result.fills) == 1 and result.fills[0].quantity == 12
    assert result.trades[0].quantity == 12 and result.trades[0].entry_price == 100
    evidence = result.metrics["corporate_actions_v1"]
    assert evidence["income"] == evidence["outstanding"] == 0
    assert len(evidence["records"]) == len(evidence["observation_revisions"]) == 1
    assert evidence["observation_revisions"][0]["terms"]["split_ratio"] == 3
    result.to_parquet(tmp_path / "result")
    assert (
        type(result).from_parquet(tmp_path / "result").metrics["corporate_actions_v1"] == evidence
    )
