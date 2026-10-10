"""Zero-share observations are not economic dividend payment obligations."""

import copy
from dataclasses import replace
from datetime import date

import polars as pl
import pytest
from test_adapter import at, tick
from test_corporate_actions import action, build

from ml4t.backtest import DataFeed, OrderStatus, Strategy
from quant_constraints import constrained_engine
from quant_constraints.corporate_actions import CorporateActionCoverage, CorporateActionProcessor
from quant_constraints.corporate_actions.processor import entitlement_key


def observe(broker, timestamp, assets, price=100):
    marks = dict.fromkeys(assets, price)
    broker._update_time(timestamp, marks, marks, marks, marks, dict.fromkeys(assets, 10000), {})


def buy(broker, asset="A"):
    tick(broker)
    order = broker.submit_order(asset, 20)
    tick(broker, at(clock="10:31"))
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.FILLED


def economic_state(broker):
    return copy.deepcopy(
        (
            broker.cash,
            broker.settled_cash,
            broker.get_account_value(),
            [
                (p.asset, p.quantity, p.entry_price, p.entry_commission)
                for p in broker.positions.values()
            ],
            broker.fills,
            broker.trades,
            broker.fee_records,
            broker.slippage_records,
            broker.controller.audit,
        )
    )


def test_zero_share_observation_has_no_payment_obligation():
    broker, _, _, _, _ = build(action("CASH_DIVIDEND"))
    tick(broker, at("2026-10-12"))
    assert not broker.account._receivables
    assert not broker.corporate_action_processor.state["entitlements"]
    record = broker.corporate_action_evidence()["records"][0]
    assert record["scope"] == "observed_only" and record["eligible_quantity"] == 0


@pytest.mark.parametrize("legacy_quantity", [False, True])
def test_credit_scans_history_only_for_legacy_quantity(legacy_quantity):
    from test_corporate_actions import held

    broker, _, _, _, _ = build(action("CASH_DIVIDEND"))
    held(broker)
    tick(broker, at("2026-10-12"), price=99)

    class CountedRecords(list):
        scans = 0

        def __iter__(self):
            self.scans += 1
            return super().__iter__()

    processor = broker.corporate_action_processor
    history = CountedRecords(
        [{"security_id": "other", "event_id": f"old-{i}", "kind": "SPLIT"} for i in range(10000)]
        + processor.state["records"]
    )
    processor.state["records"] = history
    if legacy_quantity:
        del processor.state["entitlements"][("stable-A", "event")]["quantity"]
    record = {}
    processor.credit(action("CASH_CREDIT", "credit", "2026-10-14", parent_event_id="event"), record)
    assert record["eligible_quantity"] == 20
    assert history.scans == (1 if legacy_quantity else 0)


def test_hundred_unheld_dividends_leave_no_coverage_obligation(monkeypatch):
    assets = [f"POOL-{i:03}" for i in range(100)]
    events = [
        replace(action("CASH_DIVIDEND", event_id=f"div-{asset}"), security_id=f"stable-{asset}")
        for asset in assets
    ]
    broker, _, provider, _, _ = build(*events, costs=True)
    provider.coverage += tuple(
        CorporateActionCoverage(
            f"stable-{asset}", at("2026-10-08"), date(2026, 10, 8), date(2026, 10, 12), "fixture"
        )
        for asset in assets
    )
    buy(broker, "B")
    calls, snapshot = [], provider.snapshot

    def tracked(security_id, asof):
        calls.append(security_id)
        return snapshot(security_id, asof)

    monkeypatch.setattr(provider, "snapshot", tracked)
    observe(broker, at("2026-10-12"), [*assets, "B"])
    assert set(calls) == {f"stable-{asset}" for asset in [*assets, "B"]}
    assert not broker.account._receivables
    assert not broker.corporate_action_processor.state["entitlements"]
    records = broker.corporate_action_evidence()["records"]
    assert len(records) == 100
    assert all(r["scope"] == "observed_only" and r["eligible_quantity"] == 0 for r in records)
    before = economic_state(broker)
    for day in ("2026-10-13", "2026-10-14", "2026-10-15"):
        calls.clear()
        observe(broker, at(day), ["B"])
        assert calls == ["stable-B"]
        assert economic_state(broker) == before
        assert not broker.corporate_action_processor.state["entitlements"]


@pytest.mark.parametrize("legacy_quantity", [True, False])
@pytest.mark.parametrize("restore", [True, False])
def test_legacy_zero_share_cleanup_preserves_all_economic_and_audit_state(legacy_quantity, restore):
    event = action("CASH_DIVIDEND")
    broker, _, provider, _, _ = build(event, costs=True)
    buy(broker, "B")
    tick(broker, at("2026-10-12"))
    processor = broker.corporate_action_processor
    processor.state["records"][0]["status"] = "dividend_receivable"
    item = {"net": 0.0, "gross": 0.0, "credited": False}
    if not legacy_quantity:
        item["quantity"] = 0.0
    processor.state["entitlements"][event.key] = item
    broker.account._receivables[entitlement_key(*event.key)] = 0.0
    provider.coverage = tuple(
        replace(c, covered_until=date(2026, 10, 12)) if c.security_id == "stable-A" else c
        for c in provider.coverage
    )
    checkpoint = broker._snapshot_lifecycle_state(
        all_positions=True, all_pending_orders=True, risk_rules=True, all_asset_stats=True
    )
    before = economic_state(broker)
    records = copy.deepcopy(processor.state["records"])
    revisions = copy.deepcopy(processor.state["revisions"])
    full_state = copy.deepcopy(processor.state)
    prepared = processor.prepare(at("2026-10-13"), {"B"})
    assert processor.state == full_state  # preflight is read-only
    assert broker.account._receivables == {entitlement_key(*event.key): 0.0}
    processor.apply(at("2026-10-13"), {"B"}, prepared)
    assert not processor.state["entitlements"] and not broker.account._receivables
    assert economic_state(broker) == before
    assert processor.state["records"] == records and processor.state["revisions"] == revisions
    if restore:
        broker._restore_lifecycle_state(checkpoint)
        broker.corporate_action_processor = CorporateActionProcessor(broker, provider)
    for _ in range(2):
        observe(broker, at("2026-10-13"), ["B"])
        assert not broker.corporate_action_processor.state["entitlements"]
        assert not broker.account._receivables
        assert economic_state(broker) == before
        assert broker.corporate_action_evidence()["records"] == records


@pytest.mark.parametrize("deduction", [{"withholding_rate": 1.0}, {"fee_per_share": 1.0}])
@pytest.mark.parametrize("legacy_quantity", [False, True])
def test_positive_share_zero_net_entitlement_retains_coverage_until_credit(
    monkeypatch, deduction, legacy_quantity
):
    broker, _, provider, _, _ = build(
        action("CASH_DIVIDEND", **deduction),
        action("CASH_CREDIT", "credit", "2026-10-14", parent_event_id="event", credited_net=0),
    )
    buy(broker)
    tick(broker, at("2026-10-12"), price=99)
    item = broker.corporate_action_processor.state["entitlements"][("stable-A", "event")]
    assert item == {"net": 0, "gross": 20, "quantity": 20, "credited": False}
    if legacy_quantity:
        del item["quantity"]
    assert broker.account._receivables == {entitlement_key("stable-A", "event"): 0}
    sell = broker.submit_order("A", -20)
    tick(broker, at("2026-10-12", "10:31"), price=99)
    broker._process_orders(use_open=True)
    assert sell.status is OrderStatus.FILLED and not broker.positions
    calls, snapshot = [], provider.snapshot

    def tracked(security_id, asof):
        calls.append(security_id)
        return snapshot(security_id, asof)

    monkeypatch.setattr(provider, "snapshot", tracked)
    observe(broker, at("2026-10-13"), ["B"], price=99)
    assert set(calls) == {"stable-A", "stable-B"}
    calls.clear()
    observe(broker, at("2026-10-14"), ["B"], price=99)
    assert set(calls) == {"stable-A", "stable-B"}
    assert broker.corporate_action_processor.state["entitlements"][("stable-A", "event")][
        "credited"
    ]
    assert not broker.account._receivables
    assert broker.corporate_action_evidence()["records"][-1]["eligible_quantity"] == 20
    calls.clear()
    before_cash = broker.cash
    observe(broker, at("2026-10-15"), ["B"], price=99)
    assert calls == ["stable-B"] and broker.cash == before_cash
    provider.events += (
        action("CASH_CREDIT", "duplicate", "2026-10-15", parent_event_id="event", credited_net=0),
    )
    with pytest.raises(ValueError, match="already credited"):
        tick(broker, at("2026-10-15"), price=99)
    assert broker.cash == before_cash and broker.corporate_action_evidence()["income"] == 0


@pytest.mark.parametrize(
    "problem", ["missing_proof", "net", "gross", "receivable", "negative_quantity"]
)
def test_unsafe_legacy_cleanup_fails_without_mutation(problem):
    event = action("CASH_DIVIDEND")
    broker, _, _, _, _ = build(event)
    tick(broker, at("2026-10-12"))
    processor = broker.corporate_action_processor
    item = {"quantity": 0.0, "net": 0.0, "gross": 0.0, "credited": False}
    processor.state["entitlements"][event.key] = item
    broker.account._receivables[entitlement_key(*event.key)] = 0
    if problem == "missing_proof":
        del item["quantity"]
        processor.state["records"].clear()
    elif problem == "receivable":
        broker.account._receivables[entitlement_key(*event.key)] = 1
    elif problem == "negative_quantity":
        item["quantity"] = -1
    else:
        item[problem] = 1
    before_state = copy.deepcopy(processor.state)
    before = economic_state(broker)
    receivables = dict(broker.account._receivables)
    with pytest.raises(ValueError, match="reconcile"):
        tick(broker, at("2026-10-13"))
    assert processor.state == before_state
    assert broker.account._receivables == receivables
    assert economic_state(broker) == before


def test_zero_share_migration_rolls_back_if_later_credit_fails():
    event = action("CASH_DIVIDEND")
    broker, _, provider, _, _ = build(event)
    tick(broker, at("2026-10-12"))
    processor = broker.corporate_action_processor
    processor.state["entitlements"][event.key] = {"net": 0.0, "gross": 0.0, "credited": False}
    broker.account._receivables[entitlement_key(*event.key)] = 0.0
    provider.events += (action("CASH_CREDIT", "unknown", "2026-10-13", parent_event_id="unknown"),)
    before_state = copy.deepcopy(processor.state)
    before = economic_state(broker)
    with pytest.raises(ValueError, match="Unknown/already credited"):
        tick(broker, at("2026-10-13"))
    assert processor.state == before_state
    assert broker.account._receivables == {entitlement_key(*event.key): 0}
    assert economic_state(broker) == before


def test_legacy_quantities_are_indexed_once_and_cached_on_account(monkeypatch):
    broker, _, _, _, _ = build()
    tick(broker, at("2026-10-12"))
    processor = broker.corporate_action_processor
    scans = 0
    reader = processor.legacy_entitlement_quantities
    for i in range(100):
        key = f"old-{i}", "dividend"
        processor.state["records"].append(
            {
                "security_id": key[0],
                "event_id": key[1],
                "kind": "CASH_DIVIDEND",
                "eligible_quantity": 20,
                "income_delta": 0,
            }
        )
        processor.state["entitlements"][key] = {"net": 0.0, "gross": 20.0, "credited": False}

    def counted():
        nonlocal scans
        scans += 1
        return reader()

    monkeypatch.setattr(processor, "legacy_entitlement_quantities", counted)
    quantities, empty = processor.prepare_entitlements()
    assert scans == 1 and len(quantities) == 100 and not empty
    # Apply the proven metadata backfill without replaying any event economics.
    processor.apply(at("2026-10-13"), {"B"}, ({}, [], quantities, empty))
    assert all(item["quantity"] == 20 for item in processor.state["entitlements"].values())
    assert processor.prepare_entitlements() == ({}, set()) and scans == 1
    assert broker.cash == broker.get_account_value() == 10000


@pytest.mark.parametrize("actual", [None, 0, 20])
def test_credit_without_eligible_shares_fails_closed_for_all_amounts(actual):
    broker, _, _, _, _ = build(
        action("CASH_DIVIDEND"),
        action("CASH_CREDIT", "credit", "2026-10-13", parent_event_id="event", credited_net=actual),
    )
    tick(broker, at("2026-10-12"))
    before = economic_state(broker)
    with pytest.raises(ValueError, match="Unknown/already credited dividend entitlement"):
        tick(broker, at("2026-10-13"))
    assert economic_state(broker) == before
    assert not broker.account._receivables
    assert not broker.corporate_action_processor.state["entitlements"]


def test_engine_ex_date_buy_remains_ineligible_across_revision_and_restore(tmp_path):
    event = action("CASH_DIVIDEND")
    revised = replace(event, version=2, dividend_per_share=2, available_at=at("2026-10-13"))
    _, controller, provider, config, context = build(event, revised)
    stamps = [at("2026-10-12"), at("2026-10-12", "10:31"), at("2026-10-13")]
    feed = DataFeed(
        prices_df=pl.DataFrame(
            [
                {
                    "timestamp": ts,
                    "asset": "A",
                    "open": 99.0,
                    "high": 99.0,
                    "low": 99.0,
                    "close": 99.0,
                    "volume": 10000.0,
                }
                for ts in stamps
            ]
        )
    )

    class ExternalIntent(Strategy):
        def on_data(self, timestamp, data, signals, broker):
            if timestamp == stamps[0]:
                assert broker.submit_order("A", 20).status is OrderStatus.PENDING

    engine = constrained_engine(
        feed, ExternalIntent(), controller, context, config, corporate_action_provider=provider
    )
    result = engine.run()
    assert [value for _, value in result.equity_curve] == [10000] * 3
    evidence = result.metrics["corporate_actions_v1"]
    assert evidence["income"] == evidence["outstanding"] == 0
    assert evidence["records"][0]["status"] == "observed_no_entitlement"
    assert len(evidence["records"]) == len(evidence["observation_revisions"]) == 1
    broker = engine.broker
    assert not broker.corporate_action_processor.state["entitlements"]
    checkpoint = broker._snapshot_lifecycle_state(
        all_positions=True, all_pending_orders=True, risk_rules=True, all_asset_stats=True
    )
    for restore in (False, True):
        if restore:
            broker._restore_lifecycle_state(checkpoint)
            broker.corporate_action_processor = CorporateActionProcessor(broker, provider)
        tick(broker, at("2026-10-13"), price=99)
        assert not broker.account._receivables
        assert not broker.corporate_action_processor.state["entitlements"]
        assert broker.positions["A"].quantity == 20
        assert broker.corporate_action_evidence() == evidence
    result.to_parquet(tmp_path / "result")
    assert (
        type(result).from_parquet(tmp_path / "result").metrics["corporate_actions_v1"] == evidence
    )
