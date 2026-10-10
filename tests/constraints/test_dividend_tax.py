"""Country tax selection and real broker entitlement/checkpoint reconciliation."""

import copy
from dataclasses import replace
from datetime import date

import pytest
from test_adapter import at, tick
from test_corporate_actions import action, build, held

from ml4t.backtest import OrderStatus
from quant_constraints import (
    ConstraintConfig,
    DividendTaxQualification,
    DividendTaxRule,
    resolve_dividend_tax_rate,
)


def rule(country="US", rate=0.10, **changes):
    values = {
        "source_country": country,
        "rate": rate,
        "effective_from": date(2026, 1, 1),
        "effective_to": date(2027, 1, 1),
        "known_at": at("2026-01-01"),
        "verified_at": at("2026-01-01"),
        "source": "synthetic-tax-rule",
        "version": "fixture-v1",
    }
    return DividendTaxRule(**(values | changes))


def settings(**changes):
    qualification = DividendTaxQualification(
        "CN-mainland",
        True,
        True,
        date(2026, 1, 1),
        date(2027, 1, 1),
        at("2026-01-01"),
        "synthetic-investor-scenario",
        non_us_ca_tax_resident=True,
        individual=True,
    )
    return replace(
        ConstraintConfig(),
        dividend_tax_profile="cn_mainland_individual_treaty",
        dividend_tax_scenario="synthetic_cn_treaty",
        dividend_tax_rules=(rule(), rule("CA", 0.15)),
        dividend_tax_qualification=qualification,
        **changes,
    )


def dividend(country="US", **changes):
    return action(
        "CASH_DIVIDEND",
        tax_source_country=country,
        tax_source="synthetic-issuer",
        tax_source_known_at=at("2026-01-01"),
        distribution_type="ordinary_stock",
        **changes,
    )


@pytest.mark.parametrize("country,net,tax", [("US", 18, 2), ("CA", 17, 3)])
def test_same_listing_country_rates_lock_once_and_survive_checkpoint(country, net, tax, tmp_path):
    broker, _, _, _, _ = build(
        dividend(country),
        action("CASH_CREDIT", "credit", "2026-10-13", parent_event_id="event"),
        settings=settings(),
    )
    held(broker)
    tick(broker)
    tick(broker, at("2026-10-12"), price=99)
    assert broker.cash == broker.settled_cash == 8000
    assert broker.account._receivable_value == net
    assert broker.corporate_action_evidence()["withholding"] == tax
    checkpoint = broker._snapshot_lifecycle_state(
        all_positions=True, all_pending_orders=True, risk_rules=True, all_asset_stats=True
    )
    broker._restore_lifecycle_state(checkpoint)
    # Ex-date sale cannot change the locked dividend quantity or tax.
    sale = broker.submit_order("A", -20)
    tick(broker, at("2026-10-12", "10:31"), price=99)
    broker._process_orders(use_open=True)
    assert sale.status is OrderStatus.FILLED
    before = broker.cash
    tick(broker, at("2026-10-13"), price=99)
    tick(broker, at("2026-10-13"), price=99)
    assert broker.cash == before + net
    evidence = broker.corporate_action_evidence()
    assert evidence["withholding"] == tax and broker.account._receivable_value == 0
    assert evidence["records"][0]["tax_evidence"]["source_country"] == country
    assert evidence["records"][-1]["withholding_applied_again"] is False
    # This evidence shape is serializable in the same result/Parquet route as legacy records.
    import polars as pl

    path = tmp_path / "tax.parquet"
    pl.DataFrame(evidence["records"], strict=False).write_parquet(path)
    assert pl.read_parquet(path)["withholding"].sum() == tax


@pytest.mark.parametrize(
    "change",
    [
        {"tax_source_country": None},
        {"tax_source_country": "GB"},
        {"tax_source": None},
        {"tax_source_known_at": at("2026-10-14")},
        {"distribution_type": "reit"},
        {"distribution_type": "etf"},
        {"distribution_type": "return_of_capital"},
        {"distribution_type": "mlp"},
        {"distribution_type": "adr_unknown"},
        {"currency": "CAD"},
        {"terms": "special"},
    ],
)
def test_unknown_country_or_classification_fail_before_any_bar_change(change):
    event = replace(dividend(), **change)
    broker, _, _, _, _ = build(event, settings=settings())
    held(broker)
    tick(broker)
    before = copy.deepcopy(broker.corporate_action_processor.state)
    with pytest.raises(ValueError):
        tick(broker, at("2026-10-12"), price=99)
    assert broker.corporate_action_processor.state == before
    assert broker._market_state.time == at() and broker.cash == 8000
    assert broker.positions["A"].quantity == 20


@pytest.mark.parametrize(
    "change",
    [
        {"beneficial_owner": False},
        {"non_us_ca_tax_resident": False},
        {"individual": False},
        {"treaty_documents_valid": False},
        {"tax_residence": "HK"},
        {"known_at": at("2026-10-14")},
        {"effective_to": date(2026, 10, 12)},
    ],
)
def test_no_treaty_eligibility_is_not_discount_or_zero(change):
    config = settings()
    config = replace(
        config, dividend_tax_qualification=replace(config.dividend_tax_qualification, **change)
    )
    with pytest.raises(ValueError, match="eligibility"):
        resolve_dividend_tax_rate(config, dividend(), at("2026-10-12"))


def test_profile_requires_qualification_and_actual_account_has_priority():
    with pytest.raises(ValueError, match="eligibility"):
        resolve_dividend_tax_rate(
            replace(settings(), dividend_tax_qualification=None), dividend(), at("2026-10-12")
        )
    event = dividend(
        withholding_rate=0.30,
        withholding_source="synthetic-account-statement",
        withholding_known_at=at("2026-10-08"),
    )
    rate, evidence = resolve_dividend_tax_rate(settings(), event, at("2026-10-12"))
    assert rate == 0.30 and evidence["scenario_rate_conflict"]
    with pytest.raises(ValueError, match="conflicts"):
        resolve_dividend_tax_rate(
            settings(dividend_tax_conflict_policy="reject"), event, at("2026-10-12")
        )
    for changed in (
        replace(event, withholding_source=None),
        replace(event, withholding_known_at=at("2026-10-14")),
    ):
        with pytest.raises(ValueError, match="evidence"):
            resolve_dividend_tax_rate(settings(), changed, at("2026-10-12"))


def test_validity_pit_boundaries_and_conflicting_rules():
    first = rule(effective_to=date(2026, 10, 12))
    second = rule(rate=0.2, effective_from=date(2026, 10, 12))
    config = replace(settings(), dividend_tax_rules=(first, second))
    assert resolve_dividend_tax_rate(config, dividend(), at("2026-10-12"))[0] == 0.2
    for rules in (
        (rule(), second),
        (replace(second, known_at=at("2026-10-14"), verified_at=at("2026-10-14")),),
    ):
        with pytest.raises(ValueError):
            resolve_dividend_tax_rate(
                replace(config, dividend_tax_rules=rules), dividend(), at("2026-10-12")
            )


def test_actual_credit_reconciliation_does_not_reapply_tax():
    broker, _, _, _, _ = build(
        dividend(),
        action("CASH_CREDIT", "credit", "2026-10-13", parent_event_id="event", credited_net=17.5),
        settings=settings(),
    )
    held(broker)
    tick(broker)
    tick(broker, at("2026-10-12"), price=99)
    tick(broker, at("2026-10-13"), price=99)
    assert broker.cash == 8017.5
    evidence = broker.corporate_action_evidence()
    assert evidence["withholding"] == 2
    assert evidence["income"] == 17.5
    assert evidence["records"][-1]["reconciled_net_difference"] == -0.5


def test_zero_net_real_entitlement_and_zero_quantity_observation():
    config = replace(settings(), dividend_tax_rules=(rule(rate=1),))
    broker, _, _, _, _ = build(
        dividend(),
        action("CASH_CREDIT", "credit", "2026-10-13", parent_event_id="event", credited_net=0),
        settings=config,
    )
    held(broker)
    tick(broker)
    tick(broker, at("2026-10-12"), price=99)
    assert (
        broker.corporate_action_processor.state["entitlements"][("stable-A", "event")]["quantity"]
        == 20
    )
    tick(broker, at("2026-10-13"), price=99)
    assert broker.cash == 8000 and broker.corporate_action_evidence()["withholding"] == 20
    empty, _, _, _, _ = build(dividend(None), settings=settings())
    tick(empty, at("2026-10-12"))
    assert not empty.corporate_action_processor.state["entitlements"]


@pytest.mark.parametrize("rate", [-0.1, 1.01, float("nan"), True])
def test_invalid_tax_rate_rejected(rate):
    with pytest.raises(ValueError):
        rule(rate=rate)


def test_project_profile_refuses_old_unproven_history_and_missing_investor():
    config = ConstraintConfig.from_yaml("config/us_cash_concentrated.yaml")
    with pytest.raises(ValueError, match="historical"):
        resolve_dividend_tax_rate(config, dividend(), at("2026-10-09"))
    with pytest.raises(ValueError, match="eligibility"):
        resolve_dividend_tax_rate(config, dividend(), at("2026-10-12"))


@pytest.mark.parametrize("price", [90, 110])
def test_gains_and_losses_do_not_create_capital_tax_deductions(price):
    broker, _, _, _, _ = build(settings=settings())
    held(broker)
    tick(broker, price=price)
    order = broker.submit_order("A", -20)
    tick(broker, at(clock="10:31"), price=price)
    broker._process_orders(use_open=True)
    assert order.status is OrderStatus.FILLED
    assert broker.cash == 8000 + 20 * price
    assert broker.corporate_action_evidence()["withholding"] == 0
    assert broker.corporate_action_evidence()["capital_gains_tax_mode"] == "none"


def test_explicit_statutory_scenario_does_not_claim_treaty_qualification():
    config = replace(
        settings(),
        dividend_tax_profile="explicit_rules",
        dividend_tax_qualification=None,
        dividend_tax_scenario="synthetic_statutory",
        dividend_tax_rules=(rule(rate=0.3), rule("CA", 0.25)),
    )
    assert resolve_dividend_tax_rate(config, dividend(), at("2026-10-12"))[0] == 0.3
    assert resolve_dividend_tax_rate(config, dividend("CA"), at("2026-10-12"))[0] == 0.25


def test_taxed_entitlement_split_then_bad_credit_rolls_back_every_economic_change():
    broker, _, _, _, _ = build(
        action(event_id="split"),
        dividend(),
        action(
            "CASH_CREDIT",
            "credit",
            "2026-10-12",
            parent_event_id="event",
            credited_net=100,
            available_at=at("2026-10-12"),
        ),
        settings=settings(),
    )
    held(broker)
    tick(broker)
    before = copy.deepcopy(broker.corporate_action_processor.state)
    with pytest.raises(ValueError, match="exceeds"):
        tick(broker, at("2026-10-12"), price=49.5)
    assert broker.positions["A"].quantity == 20 and broker.positions["A"].entry_price == 100
    assert broker.cash == 8000 and not broker.account._receivables
    assert broker._market_state.time == at()
    assert broker.corporate_action_processor.state == before


def test_security_rename_and_split_tax_rule_remain_bound_to_stable_event():
    broker, _, _, _, _ = build(action(event_id="split"), dividend("CA"), settings=settings())
    held(broker)
    tick(broker)
    tick(broker, at("2026-10-12"), price=49.5)
    record = broker.corporate_action_evidence()["records"][-1]
    assert record["security_id"] == "stable-A" and record["eligible_quantity"] == 40
    assert record["withholding"] == 6 and broker.account._receivable_value == 34
    # The selector has no ticker, quote currency, listing or execution-venue lookup.
    changed_name = replace(dividend("CA"), event_id="renamed-source-event")
    assert resolve_dividend_tax_rate(settings(), changed_name, at("2026-10-12"))[0] == 0.15


def test_plain_sector_etf_cannot_be_misclassified_as_ordinary_stock_dividend():
    broker, _, _, _, original_context = build(dividend(), settings=settings())
    broker.context_provider = lambda ts, asset, phase: replace(
        original_context(ts, asset, phase), instrument="plain_sector_etf"
    )
    held(broker)
    tick(broker)
    with pytest.raises(ValueError, match="ETF distributions"):
        tick(broker, at("2026-10-12"), price=99)
    assert broker.cash == 8000 and not broker.account._receivables
