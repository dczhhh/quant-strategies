"""Official Tiered examples, order lifecycles, side/routing and pure estimates."""

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime

import pytest
from test_adapter import at

from quant_constraints import FeeExecutionContext, IBKRProTieredUSStock, RegulatoryRate


def context(order="one", day="2026-10-09", side="BUY", **kwargs):
    return FeeExecutionContext(order, at(day), side, **kwargs)


def filled(model, identity, quantity, price=100, ctx=None):
    quote = model.quote(ctx or context(identity), quantity, price)
    return model.commit(identity, quote)


def test_first_100_shares_nonzero_and_third_party_components_separate():
    model = IBKRProTieredUSStock()
    record = filled(model, "first", 100)
    fees = record.fees
    assert fees.broker_commission == pytest.approx(0.35)
    assert fees.exchange_ecn_fees_or_rebates == pytest.approx(0.35)
    assert fees.clearing_fees == pytest.approx(0.02)
    assert fees.cat_fees == pytest.approx(0.0003)
    assert fees.sec_fees == fees.taf_fees == 0
    assert fees.pass_through_fees == pytest.approx(0.35 * 0.000735)
    assert fees.total_fees == pytest.approx(0.72055725)
    assert model.monthly_volumes == {"2026-10": 100}
    assert len(fees.sources) >= 4 and "holiday" in fees.rate_version
    assert any("unknown venue" in x for x in fees.assumptions)


def test_boundary_marginal_299900_plus200_not_whole_order_lower_rate():
    model = IBKRProTieredUSStock(initial_month="2026-10", initial_monthly_volume=299900)
    record = filled(model, "cross", 200)
    assert record.fees.broker_commission == pytest.approx(100 * 0.0035 + 100 * 0.002)
    assert record.fees.monthly_volume_before == 299900
    assert model.volume("2026-10") == 300100


@pytest.mark.parametrize(
    ("quantity", "fee"),
    [
        (3_000_100, 1050 + 5400 + 0.15),
        (30_000_000, 1050 + 5400 + 25500 + 10000),
        (100_000_100, 1050 + 5400 + 25500 + 80000 + 0.05),
    ],
)
def test_large_order_crosses_multiple_tiers(quantity, fee):
    assert filled(
        IBKRProTieredUSStock(), "huge", quantity, 5
    ).fees.broker_commission == pytest.approx(fee)


def test_us_eastern_month_reset_not_utc_midnight():
    model = IBKRProTieredUSStock(initial_month="2026-10", initial_monthly_volume=300000)
    stamp = datetime.fromisoformat("2026-11-01T03:59:00+00:00")
    first = filled(model, "oct", 1000, ctx=replace(context("oct"), timestamp=stamp))
    second = filled(
        model, "nov", 1000, ctx=replace(context("nov"), timestamp=stamp.replace(hour=4, minute=0))
    )
    assert first.fees.broker_commission == 2
    assert second.fees.broker_commission == 3.5
    assert model.monthly_volumes == {"2026-10": 301000, "2026-11": 1000}


def test_distinct_assets_and_buy_sell_share_same_monthly_volume():
    model = IBKRProTieredUSStock(initial_month="2026-10", initial_monthly_volume=299900)
    buy = filled(model, "stock-A", 100)
    sell = filled(model, "stock-B", 1000, ctx=context("stock-B", side="SELL"))
    assert buy.fees.broker_commission == pytest.approx(0.35)
    assert sell.fees.broker_commission == 2
    assert sell.fees.sec_fees == pytest.approx(100000 * 0.0000206)
    assert model.volume("2026-10") == 301000


def test_partial_fills_share_one_order_minimum_not_one_per_fill():
    model = IBKRProTieredUSStock()
    ctx = context()
    first = filled(model, "part1", 10, ctx=ctx)
    second = filled(model, "part2", 90, ctx=ctx)
    third = filled(model, "part3", 100, ctx=ctx)
    assert first.fees.broker_commission == 0.35
    assert second.fees.broker_commission == 0
    assert third.fees.broker_commission == pytest.approx(0.35)
    assert sum(r.fees.broker_commission for r in model.records) == pytest.approx(0.7)
    assert model.volume("2026-10") == 200


@pytest.mark.parametrize("change", ["amendment", "overnight"])
def test_minimum_restarts_for_successful_replacement_or_new_day(change):
    model = IBKRProTieredUSStock()
    first = filled(model, "part1", 10)
    ctx = (
        replace(first.context, generation=1)
        if change == "amendment"
        else context("part1", "2026-10-13")
    )
    second = filled(model, "part2", 10, ctx=ctx)
    assert first.fees.broker_commission == second.fees.broker_commission == 0.35


@pytest.mark.parametrize(
    ("quantity", "price", "base", "eligible"),
    [
        (10, 0.2, 0.02, 0),
        (100000, 0.1, 100, 0),
        (0.5, 10, 0.05, 0.5),
        (0.05, 15, 0.01, 0.05),
        (0.001, 0.01, 0.01, 0.001),
        (1.5, 100, 0.85, 1.5),
    ],
)
def test_low_price_cap_fractional_minimum_and_no_round_to_zero(quantity, price, base, eligible):
    model = IBKRProTieredUSStock()
    record = filled(model, "small", quantity, price)
    assert record.fees.broker_commission == pytest.approx(base)
    assert record.fees.total_fees > 0
    assert model.volume("2026-10") == pytest.approx(eligible)
    assert record.fees.clearing_fees <= quantity * price * 0.005 + 1e-12


def test_capped_partial_order_does_not_advance_volume_and_quote_has_no_side_effects():
    model = IBKRProTieredUSStock()
    for index in range(3):
        filled(model, str(index), 10, 0.2, context("same"))
    assert model.volume("2026-10") == 0
    assert sum(r.fees.broker_commission for r in model.records) == pytest.approx(0.06)
    assert all(r.fees.commission_cap_applied for r in model.records)


def test_quote_estimate_deepcopy_idempotency_stale_quote_and_conflict():
    model = IBKRProTieredUSStock(initial_month="2026-10", initial_monthly_volume=299900)
    ctx = context()
    quote = model.quote(ctx, 200, 100)
    for _ in range(20):
        assert model.quote(ctx, 200, 100) == quote
        assert model.estimate(ctx, 200, 100).broker_commission == pytest.approx(0.7)
        deepcopy(model).estimate(ctx, 200, 100)
    assert model.records == () and dict(model.monthly_volumes) == {}
    record = model.commit("execution", quote)
    assert model.commit("execution", quote) == record
    assert model.volume("2026-10") == 300100 and len(model.records) == 1
    with pytest.raises(ValueError, match="Stale"):
        model.commit("retry-different-id", quote)
    with pytest.raises(ValueError, match="Conflicting"):
        model.commit("execution", model.quote(context("another"), 100, 100))
    assert len(model.records) == 1


@pytest.mark.parametrize(
    ("venue", "liquidity", "price", "expected"),
    [
        ("NASDAQ", "remove", 100, 0.3),
        ("ARCA", "remove", 100, 0.3),
        ("IEX", "remove", 0.5, 0.1),
        ("NASDAQ", "remove", 0.5, 0.15),
        ("IEX", "add", 100, 0),
        ("ARCA", "add", 100, 0),
        ("unknown", "add", 100, 0.35),
        ("ARCA", "unknown", 100, 0.35),
    ],
)
def test_venue_liquidity_explicit_conservative_unknown_and_no_assumed_rebate(
    venue, liquidity, price, expected
):
    fees = filled(
        IBKRProTieredUSStock(),
        "venue",
        100,
        price,
        context("venue", venue=venue, liquidity=liquidity),
    ).fees
    assert fees.exchange_ecn_fees_or_rebates == pytest.approx(expected)
    assert fees.exchange_ecn_fees_or_rebates >= 0


@pytest.mark.parametrize(
    ("day", "sec", "taf"),
    [
        ("2026-04-03", 0, 0.0195),
        ("2026-04-04", 0.206, 0.0195),
        ("2026-09-30", 0.206, 0.0195),
        ("2026-10-01", 0.206, 0),
        ("2026-12-31", 0.206, 0),
    ],
)
def test_regulatory_effective_dates_and_taf_holiday_sells_only(day, sec, taf):
    model = IBKRProTieredUSStock()
    sell = filled(model, "sell", 100, ctx=context("sell", day, "SELL"))
    buy = filled(model, "buy", 100, ctx=context("buy", day))
    assert sell.fees.sec_fees == pytest.approx(sec)
    assert sell.fees.taf_fees == pytest.approx(taf)
    assert buy.fees.sec_fees == buy.fees.taf_fees == 0
    assert sell.fees.cat_fees == buy.fees.cat_fees == pytest.approx(0.0003)


def test_taf_per_trade_cap_and_explicit_historical_versions():
    model = IBKRProTieredUSStock()
    assert (
        filled(
            model, "large-sell", 100000, ctx=context("large-sell", "2026-09-30", "SELL")
        ).fees.taf_fees
        == 9.79
    )
    with pytest.raises(ValueError, match="No explicit regulatory"):
        IBKRProTieredUSStock().quote(context(day="2024-05-01"), 100, 100)
    history = RegulatoryRate(
        date(2024, 1, 1),
        date(2024, 12, 31),
        0.000008,
        0.000166,
        8.3,
        cat_per_share=0,
        version="explicit-fixture-not-production-history",
    )
    fees = (
        IBKRProTieredUSStock(regulatory_rates=(history,))
        .quote(context(day="2024-05-01", side="SELL"), 100, 100)
        .fees
    )
    assert fees.sec_fees == 0.08 and fees.cat_fees == 0
    assert fees.rate_version == history.version


def test_external_us_canada_stock_etf_volume_scope_and_no_external_fees():
    model = IBKRProTieredUSStock()
    for identity, market, instrument, tiered, capped in [
        ("us", "US", "stock", True, False),
        ("ca", "CA", "etf", True, False),
        ("fixed", "US", "stock", False, False),
        ("capped", "CA", "stock", True, True),
    ]:
        model.record_external_volume(
            identity,
            at(),
            150000,
            market=market,
            instrument=instrument,
            tiered=tiered,
            capped=capped,
        )
    model.record_external_volume("ca", at(), 150000, market="CA", instrument="etf")
    assert model.records == () and model.volume("2026-10") == 300000
    assert len(model.external_executions) == 4
    with pytest.raises(TypeError):
        model.external_executions["made-up"] = ()
    assert filled(model, "simulated", 1000).fees.broker_commission == 2
    with pytest.raises(ValueError, match="Conflicting"):
        model.record_external_volume("ca", at(), 10, market="CA", instrument="etf")
    with pytest.raises(ValueError, match="US/CA"):
        model.record_external_volume("uk", at(), 10, market="UK", instrument="stock")


@pytest.mark.parametrize(
    "settings",
    [
        {"initial_monthly_volume": 1},
        {"initial_monthly_volume": -1},
        {"unknown_venue_per_share": float("nan")},
        {"initial_month": "2026-1"},
        {"unknown_venue_rate": -1},
    ],
)
def test_invalid_model_assumptions_fail_explicitly(settings):
    with pytest.raises(ValueError):
        IBKRProTieredUSStock(**settings)


@pytest.mark.parametrize(
    "settings",
    [
        {"routing": "direct"},
        {"side": "SHORT"},
        {"market": "CA"},
        {"liquidity": "maybe"},
        {"generation": -1},
        {"instrument": "option"},
    ],
)
def test_invalid_execution_context(settings):
    with pytest.raises(ValueError):
        replace(context(), **settings)


@pytest.mark.parametrize(("quantity", "price"), [(0, 100), (-1, 100), (1, 0), (1, float("nan"))])
def test_invalid_quotes_leave_ledger_unchanged(quantity, price):
    model = IBKRProTieredUSStock()
    with pytest.raises(ValueError):
        model.quote(context(), quantity, price)
    assert model.records == () and dict(model.monthly_volumes) == {}


def test_forged_quote_unordered_executions_and_overlapping_versions_are_atomic():
    model = IBKRProTieredUSStock()
    quote = model.quote(context(), 100, 100)
    with pytest.raises(ValueError, match="current execution"):
        model.commit("forged", replace(quote, fees=replace(quote.fees, broker_commission=0)))
    assert model.records == () and model.volume("2026-10") == 0
    model.commit("real", quote)
    with pytest.raises(ValueError, match="chronological"):
        model.quote(context(day="2026-10-08"), 100, 100)
    with pytest.raises(ValueError, match="chronological"):
        model.record_external_volume("past", at("2026-10-08"), 100, market="CA", instrument="etf")
    assert model.volume("2026-10") == 100 and len(model.records) == 1
    rate = RegulatoryRate(date(2026, 1, 1), date(2026, 12, 31), 0, 0, 0)
    with pytest.raises(ValueError, match="Overlapping"):
        IBKRProTieredUSStock(regulatory_rates=(rate, rate))
    with pytest.raises(ValueError, match="interval"):
        replace(rate, end=date(2025, 1, 1))
    with pytest.raises(ValueError, match="version and sources"):
        replace(rate, sources=())
