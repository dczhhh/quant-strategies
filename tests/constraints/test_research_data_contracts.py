"""Offline contract acceptance/rejection; no provider, account or strategy signals."""

import copy
from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import UTC, date, datetime

import pytest

from quant_constraints.research.data.contracts import (
    SCHEMA_VERSION,
    ActionKind,
    ContractError,
    CorporateActionRecord,
    CreditEvidence,
    ErrorCode,
    IdentityMap,
    IdentityTransition,
    PriceBasis,
    RawBar,
    SecurityIdentity,
    ShareUnit,
    SignalBar,
    SourceProvenance,
    VolumeUnit,
    validate_price_risk_input,
)
from quant_constraints.research.data.normalize import (
    normalize_identities,
    normalize_raw_bars,
    normalize_record,
    normalize_signal_bars,
)


def provenance(**changes):
    return {
        "provider": "offline-fixture",
        "dataset_id": "synthetic-bars",
        "dataset_revision": "r1",
        "record_id": "bar-1",
        "record_version": 1,
        "ingested_at": "2026-10-10T10:00:00Z",
        "source_uri": "fixture://synthetic-bars/r1",
        "source_timezone": "America/New_York",
    } | changes


def bar(**changes):
    return {
        "security_id": "fixture-security-A",
        "symbol": "AAA",
        "venue": "XNAS",
        "currency": "USD",
        "product_id": "raw-minutes",
        "bar_start_at": "2024-06-07T09:30:00-04:00",
        "bar_end_at": "2024-06-07T09:31:00-04:00",
        "available_at": "2024-06-07T09:31:01-04:00",
        "open": 100,
        "high": 101,
        "low": 99,
        "close": 100.5,
        "volume": 100.25,
        "price_basis": "raw",
        "share_unit": "as_traded",
        "volume_unit": "as_traded_shares",
        "source": provenance(),
    } | changes


def signal(**changes):
    return bar(product_id="signal-minutes", raw_product_id="raw-minutes") | changes


def identity(**changes):
    return {
        "security_id": "fixture-security-A",
        "symbol": "AAA",
        "venue": "XNAS",
        "currency": "USD",
        "effective_from": "2020-01-01T00:00:00Z",
        "effective_until": None,
        "transition": "listing",
        "source": provenance(record_id="identity-A"),
    } | changes


def action(**changes):
    return {
        "security_id": "fixture-security-A",
        "event_id": "split-1",
        "kind": "split",
        "effective_date": "2024-06-10",
        "historically_known_at": "2024-05-22T20:00:00Z",
        "knowledge_evidence": "fixture://public-release",
        "event_version": 1,
        "source": provenance(record_id="action-1"),
        "currency": "USD",
        "split_ratio": 10,
    } | changes


def dividend(**changes):
    record = action(kind="cash_dividend", event_id="dividend-1", split_ratio=None)
    return (
        record
        | {
            "dividend_per_share": 1,
            "ex_date": "2024-06-10",
            "record_date": "2024-06-10",
            "payable_date": "2024-06-17",
        }
        | changes
    )


def credit(**changes):
    return (
        action(kind="cash_credit", event_id="credit-1", split_ratio=None)
        | {
            "effective_date": "2024-06-17",
            "historically_known_at": "2024-06-17T15:00:00Z",
            "credit_date": "2024-06-17",
            "parent_event_id": "dividend-1",
            "credited_net": 20,
            "credit_evidence": "broker_confirmation",
            "credit_evidence_ref": "fixture://broker-statement/credit-1",
        }
        | changes
    )


def reject(code, callback):
    with pytest.raises(ContractError) as exc:
        callback()
    assert exc.value.code is code
    assert exc.value.as_dict()["schema_version"] == SCHEMA_VERSION
    return exc.value


def test_normalization_is_immutable_deterministic_and_preserves_source_clocks():
    record = bar()
    original = copy.deepcopy(record)
    raw = normalize_record(RawBar, record)
    assert record == original
    assert raw.bar_start_at == datetime(2024, 6, 7, 13, 30, tzinfo=UTC)
    assert raw.bar_end_at < raw.available_at < raw.source.ingested_at
    assert raw.source.source_timezone == "America/New_York"
    assert raw.volume == 100.25
    assert raw == normalize_record(RawBar, dict(reversed(list(record.items()))))
    assert raw == normalize_record(RawBar, asdict(raw))
    with pytest.raises(FrozenInstanceError):
        raw.close = 1
    with pytest.raises(FrozenInstanceError):
        raw.source.record_version = 2


@pytest.mark.parametrize(
    "record_type, factory",
    [
        (RawBar, bar),
        (SourceProvenance, provenance),
        (SecurityIdentity, identity),
        (CorporateActionRecord, action),
    ],
)
def test_each_required_field_has_a_stable_missing_field_diagnostic(record_type, factory):
    from dataclasses import MISSING

    for field in fields(record_type):
        if field.default is MISSING and field.default_factory is MISSING:
            record = factory()
            del record[field.name]
            error = reject(
                ErrorCode.MISSING_FIELD, lambda record=record: normalize_record(record_type, record)
            )
            assert error.field == field.name


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True, "100", None, 10**1000])
def test_bad_prices_reject_without_repair(value):
    code = (
        ErrorCode.INVALID_TYPE
        if value is None or isinstance(value, str | bool)
        else ErrorCode.INVALID_VALUE
    )
    reject(code, lambda: normalize_record(RawBar, bar(open=value)))


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("volume", -1, ErrorCode.INVALID_VALUE),
        ("volume", False, ErrorCode.INVALID_TYPE),
        ("volume", float("nan"), ErrorCode.INVALID_VALUE),
        ("high", 99.5, ErrorCode.INVALID_OHLCV),
        ("low", 100.25, ErrorCode.INVALID_OHLCV),
        ("close", 102, ErrorCode.INVALID_OHLCV),
        ("currency", "usd", ErrorCode.INVALID_VALUE),
        ("currency", None, ErrorCode.INVALID_VALUE),
        ("symbol", " AAA", ErrorCode.INVALID_VALUE),
        ("venue", "", ErrorCode.INVALID_VALUE),
        ("price_basis", None, ErrorCode.INVALID_VALUE),
        ("price_basis", "adjusted", ErrorCode.INVALID_VALUE),
        ("share_unit", "unknown", ErrorCode.INVALID_VALUE),
        ("volume_unit", None, ErrorCode.INVALID_VALUE),
        ("source", None, ErrorCode.INVALID_TYPE),
        ("schema_version", "research_data_v2", ErrorCode.UNSUPPORTED_SCHEMA),
    ],
)
def test_bar_structure_rejects_invalid_fields(field, value, code):
    reject(code, lambda: normalize_record(RawBar, bar(**{field: value})))


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("bar_start_at", "2024-06-07T09:30:00", ErrorCode.NAIVE_TIMESTAMP),
        ("bar_start_at", "not-a-time", ErrorCode.INVALID_VALUE),
        ("bar_start_at", 1, ErrorCode.INVALID_TYPE),
        ("bar_end_at", "2024-06-07T09:30:00-04:00", ErrorCode.TIME_ORDER),
        ("bar_end_at", "2024-06-07T09:29:00-04:00", ErrorCode.TIME_ORDER),
        ("available_at", "2024-06-07T09:30:59-04:00", ErrorCode.TIME_ORDER),
        ("available_at", "2027-01-01T00:00:00Z", ErrorCode.TIME_ORDER),
    ],
)
def test_impossible_bar_clocks_reject(field, value, code):
    reject(code, lambda: normalize_record(RawBar, bar(**{field: value})))


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("record_version", 0, ErrorCode.INVALID_VALUE),
        ("record_version", True, ErrorCode.INVALID_VALUE),
        ("record_id", None, ErrorCode.INVALID_VALUE),
        ("provider", "", ErrorCode.INVALID_VALUE),
        ("dataset_revision", "", ErrorCode.INVALID_VALUE),
        ("source_uri", "", ErrorCode.INVALID_VALUE),
        ("source_timezone", "Mars/Unknown", ErrorCode.INVALID_VALUE),
        ("source_timezone", "/etc/localtime", ErrorCode.INVALID_VALUE),
        ("ingested_at", "2026-10-10T10:00:00", ErrorCode.NAIVE_TIMESTAMP),
    ],
)
def test_source_metadata_cannot_be_missing_or_ambiguous(field, value, code):
    reject(code, lambda: normalize_record(SourceProvenance, provenance(**{field: value})))


def test_unknown_fields_and_claimed_verification_cannot_enter_contract():
    reject(ErrorCode.UNKNOWN_FIELD, lambda: normalize_record(RawBar, bar(verified=True)))
    reject(
        ErrorCode.UNKNOWN_FIELD,
        lambda: normalize_record(SourceProvenance, provenance(verified=True)),
    )
    reject(ErrorCode.INVALID_TYPE, lambda: normalize_record(RawBar, []))
    reject(ErrorCode.INVALID_TYPE, lambda: normalize_record(dict, {}))
    reject(ErrorCode.INVALID_TYPE, lambda: normalize_record(RawBar, {1: "x"}))
    reject(ErrorCode.MISSING_FIELD, lambda: normalize_record(RawBar, bar(source={})))
    first = reject(
        ErrorCode.INVALID_VALUE,
        lambda: normalize_record(RawBar, bar(price_basis="bad", share_unit="bad")),
    )
    second = reject(
        ErrorCode.INVALID_VALUE,
        lambda: normalize_record(
            RawBar, dict(reversed(list(bar(price_basis="bad", share_unit="bad").items())))
        ),
    )
    assert str(first) == str(second)


@pytest.mark.parametrize(
    "changes",
    [
        {"price_basis": "split_adjusted"},
        {"price_basis": "total_return"},
        {"share_unit": "split_adjusted"},
        {"volume_unit": "split_adjusted_shares"},
        {"adjustment_version": "factor-r1"},
    ],
)
def test_raw_bar_cannot_accept_signal_adjustments(changes):
    reject(ErrorCode.UNIT_MISMATCH, lambda: normalize_record(RawBar, bar(**changes)))


@pytest.mark.parametrize("basis", ["raw", "split_adjusted", "total_return"])
@pytest.mark.parametrize("volume_unit", ["as_traded_shares", "split_adjusted_shares"])
def test_signal_products_require_explicit_price_share_and_volume_policy(basis, volume_unit):
    changes = {"price_basis": basis, "volume_unit": volume_unit}
    if basis != "raw":
        changes |= {"share_unit": "split_adjusted", "adjustment_version": "factor-r1"}
    if basis == "raw" and volume_unit != "as_traded_shares":
        reject(ErrorCode.UNIT_MISMATCH, lambda: normalize_record(SignalBar, signal(**changes)))
    else:
        result = normalize_record(SignalBar, signal(**changes))
        assert result.price_basis.value == basis
        assert result.volume_unit.value == volume_unit


@pytest.mark.parametrize(
    "changes",
    [
        {"product_id": "raw-minutes"},
        {"raw_product_id": ""},
        {"price_basis": "split_adjusted"},
        {"price_basis": "total_return", "adjustment_version": "r1"},
        {"adjustment_version": "r1"},
        {"share_unit": "split_adjusted"},
    ],
)
def test_signal_products_reject_incomplete_or_overwriting_declarations(changes):
    code = ErrorCode.INVALID_VALUE if changes == {"raw_product_id": ""} else ErrorCode.UNIT_MISMATCH
    reject(code, lambda: normalize_record(SignalBar, signal(**changes)))


@pytest.mark.parametrize(
    "changes",
    [
        {"currency": "EUR"},
        {"security_id": "other"},
        {"venue": "XNYS"},
        {"bar_start_at": "2024-06-07T09:30:01-04:00"},
        {"bar_end_at": "2024-06-07T09:31:01-04:00"},
        {
            "price_basis": "split_adjusted",
            "share_unit": "split_adjusted",
            "adjustment_version": "r1",
        },
    ],
)
def test_price_risk_must_use_the_execution_units_identity_and_interval(changes):
    execution = normalize_record(RawBar, bar())
    risk = normalize_record(SignalBar, signal(**changes))
    reject(ErrorCode.UNIT_MISMATCH, lambda: validate_price_risk_input(execution, risk))


def test_raw_risk_accepts_matching_units_and_rejects_future_availability():
    execution = normalize_record(RawBar, bar())
    risk = normalize_record(SignalBar, signal())
    validate_price_risk_input(execution, execution)
    validate_price_risk_input(execution, risk)
    later = replace(risk, available_at=datetime(2024, 6, 7, 13, 32, tzinfo=UTC))
    reject(ErrorCode.TIME_ORDER, lambda: validate_price_risk_input(execution, later))
    reject(ErrorCode.INVALID_TYPE, lambda: validate_price_risk_input(risk, execution))
    reject(ErrorCode.INVALID_TYPE, lambda: validate_price_risk_input(execution, None))


def test_bar_batch_reorders_equivalently_without_modifying_input():
    identities = normalize_identities([identity()])
    first = bar()
    second = bar(
        bar_start_at="2024-06-07T13:31:00Z",
        bar_end_at="2024-06-07T13:32:00Z",
        available_at="2024-06-07T13:32:00Z",
        source=provenance(record_id="bar-2"),
    )
    records = [second, first]
    original = copy.deepcopy(records)
    assert normalize_raw_bars(records, identities) == normalize_raw_bars(
        reversed(records), identities
    )
    assert records == original
    assert normalize_raw_bars([], identities) == ()
    assert len(normalize_signal_bars([signal()], identities)) == 1


@pytest.mark.parametrize(
    "changes,code",
    [
        ({}, ErrorCode.DUPLICATE_BAR),
        ({"bar_start_at": "2024-06-07T13:30:00Z"}, ErrorCode.DUPLICATE_BAR),
        (
            {"product_id": "competing-raw", "source": provenance(record_id="different")},
            ErrorCode.DUPLICATE_BAR,
        ),
        (
            {
                "bar_start_at": "2024-06-07T13:30:30Z",
                "bar_end_at": "2024-06-07T13:31:30Z",
                "available_at": "2024-06-07T13:31:31Z",
                "source": provenance(record_id="different"),
            },
            ErrorCode.OVERLAPPING_BAR,
        ),
        (
            {
                "bar_start_at": "2024-06-07T13:31:00Z",
                "bar_end_at": "2024-06-07T13:32:00Z",
                "available_at": "2024-06-07T13:32:00Z",
            },
            ErrorCode.DUPLICATE_BAR,
        ),
    ],
)
def test_duplicate_and_overlapping_bars_fail_closed(changes, code):
    identities = normalize_identities([identity()])
    reject(code, lambda: normalize_raw_bars([bar(), bar(**changes)], identities))


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"security_id": "other"}, ErrorCode.IDENTITY_CONFLICT),
        ({"currency": "EUR"}, ErrorCode.IDENTITY_CONFLICT),
        ({"symbol": "UNKNOWN"}, ErrorCode.IDENTITY_MISSING),
        ({"venue": "XNYS"}, ErrorCode.IDENTITY_MISSING),
    ],
)
def test_bars_require_identity_coverage(changes, code):
    identities = normalize_identities([identity()])
    reject(code, lambda: normalize_raw_bars([bar(**changes)], identities))


def test_identity_intervals_are_half_open_and_bar_cannot_cross_rename():
    boundary = "2024-06-07T13:31:00Z"
    old = identity(effective_until=boundary)
    new = identity(symbol="NEW", effective_from=boundary, transition="rename")
    mapping = normalize_identities([new, old])
    assert mapping == normalize_identities([old, new])
    at = datetime.fromisoformat(boundary)
    assert mapping.resolve_effective("NEW", "XNAS", at).security_id == "fixture-security-A"
    reject(ErrorCode.IDENTITY_MISSING, lambda: mapping.resolve_effective("AAA", "XNAS", at))
    assert len(normalize_raw_bars([bar()], mapping)) == 1  # end exactly at boundary is legal
    crossing = bar(bar_end_at="2024-06-07T13:31:01Z")
    reject(ErrorCode.IDENTITY_CONFLICT, lambda: normalize_raw_bars([crossing], mapping))
    reject(
        ErrorCode.NAIVE_TIMESTAMP,
        lambda: mapping.resolve_effective("AAA", "XNAS", datetime(2024, 1, 1)),
    )


@pytest.mark.parametrize(
    "changes,code",
    [
        ({}, ErrorCode.IDENTITY_CONFLICT),
        ({"symbol": "NEW", "transition": "rename"}, ErrorCode.IDENTITY_CONFLICT),
        ({"security_id": "other"}, ErrorCode.UNSUPPORTED_IDENTITY),
        ({"transition": "merger"}, ErrorCode.UNSUPPORTED_IDENTITY),
        ({"transition": "delisting"}, ErrorCode.UNSUPPORTED_IDENTITY),
        ({"effective_until": "2020-01-01T00:00:00Z"}, ErrorCode.TIME_ORDER),
        ({"effective_from": "2020-01-01T00:00:00"}, ErrorCode.NAIVE_TIMESTAMP),
    ],
)
def test_conflicting_or_unsupported_identity_is_rejected(changes, code):
    reject(code, lambda: normalize_identities([identity(), identity(**changes)]))


def test_nonoverlapping_ticker_reuse_is_explicitly_unsupported():
    old = identity(effective_until="2024-01-01T00:00:00Z")
    new = identity(security_id="other", effective_from="2024-01-01T00:00:00Z")
    reject(ErrorCode.UNSUPPORTED_IDENTITY, lambda: normalize_identities([old, new]))
    reject(ErrorCode.INVALID_TYPE, lambda: IdentityMap([normalize_record(SecurityIdentity, old)]))
    reject(ErrorCode.INVALID_TYPE, lambda: IdentityMap((None,)))
    reject(ErrorCode.INVALID_TYPE, lambda: normalize_raw_bars([bar()], None))


@pytest.mark.parametrize(
    "changes",
    [
        {"transition": "listing"},
        {"symbol": "AAA"},
        {"effective_from": "2024-01-02T00:00:00Z"},
        {"venue": "XNYS"},
        {"currency": "EUR"},
    ],
)
def test_rename_requires_a_contiguous_explicit_chain(changes):
    old = identity(effective_until="2024-01-01T00:00:00Z")
    new = (
        identity(symbol="NEW", effective_from="2024-01-01T00:00:00Z", transition="rename") | changes
    )
    reject(ErrorCode.IDENTITY_CONFLICT, lambda: normalize_identities([old, new]))


def test_orphan_rename_and_wrong_risk_raw_product_reject():
    reject(
        ErrorCode.IDENTITY_CONFLICT, lambda: normalize_identities([identity(transition="rename")])
    )
    execution = normalize_record(RawBar, bar())
    risk = normalize_record(SignalBar, signal(raw_product_id="unrelated-raw"))
    reject(ErrorCode.UNIT_MISMATCH, lambda: validate_price_risk_input(execution, risk))


@pytest.mark.parametrize(
    "changes",
    [
        {"historically_known_at": "2024-06-16T23:00:00Z"},
        {
            "historically_known_at": None,
            "knowledge_evidence": None,
            "source": provenance(ingested_at="2024-06-16T23:00:00Z"),
        },
    ],
)
def test_broker_confirmation_cannot_be_available_before_actual_credit_date(changes):
    reject(ErrorCode.TIME_ORDER, lambda: normalize_record(CorporateActionRecord, credit(**changes)))


def test_simulated_credit_is_not_mislabeled_as_actual_confirmation():
    simulated = credit(
        credit_evidence="simulated_assumption",
        historically_known_at=None,
        knowledge_evidence=None,
        source=provenance(ingested_at="2024-06-16T23:00:00Z"),
    )
    assert (
        normalize_record(CorporateActionRecord, simulated).credit_evidence
        is CreditEvidence.SIMULATED_ASSUMPTION
    )


@pytest.mark.parametrize("ratio", [10, 4, 0.1, 1])
def test_split_keeps_effective_public_and_ingestion_times_distinct(ratio):
    event = normalize_record(CorporateActionRecord, action(split_ratio=ratio))
    assert event.kind is ActionKind.SPLIT
    assert event.split_ratio == ratio
    assert event.effective_date == date(2024, 6, 10)
    assert event.historically_known_at == datetime(2024, 5, 22, 20, tzinfo=UTC)
    assert event.source.ingested_at.year == 2026
    assert event == normalize_record(CorporateActionRecord, asdict(event))


def test_unknown_historical_public_time_is_not_replaced_by_ingestion_or_effective_time():
    event = normalize_record(
        CorporateActionRecord, action(historically_known_at=None, knowledge_evidence=None)
    )
    assert event.historically_known_at is None
    assert event.knowledge_evidence is None
    late = normalize_record(
        CorporateActionRecord, action(historically_known_at="2024-07-01T00:00:00Z")
    )
    assert late.historically_known_at.date() > late.effective_date


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"historically_known_at": "2027-01-01T00:00:00Z"}, ErrorCode.TIME_ORDER),
        ({"historically_known_at": "2024-01-01T00:00:00"}, ErrorCode.NAIVE_TIMESTAMP),
        ({"historically_known_at": None}, ErrorCode.KNOWLEDGE_EVIDENCE),
        ({"knowledge_evidence": None}, ErrorCode.KNOWLEDGE_EVIDENCE),
        ({"knowledge_evidence": ""}, ErrorCode.INVALID_VALUE),
        ({"event_version": 0}, ErrorCode.INVALID_VALUE),
        ({"event_version": True}, ErrorCode.INVALID_VALUE),
        ({"event_version": 2}, ErrorCode.ACTION_TERMS),
        ({"revision_of": 1}, ErrorCode.ACTION_TERMS),
        ({"revision_of": 0}, ErrorCode.INVALID_VALUE),
        ({"cancelled": 1}, ErrorCode.INVALID_TYPE),
        ({"effective_date": "bad-date"}, ErrorCode.INVALID_VALUE),
        ({"effective_date": datetime(2024, 6, 10, tzinfo=UTC)}, ErrorCode.INVALID_TYPE),
        ({"effective_date": None}, ErrorCode.INVALID_TYPE),
        ({"split_ratio": None}, ErrorCode.ACTION_TERMS),
        ({"split_ratio": 0}, ErrorCode.INVALID_VALUE),
        ({"split_ratio": float("nan")}, ErrorCode.INVALID_VALUE),
        ({"dividend_per_share": 1}, ErrorCode.ACTION_TERMS),
        ({"kind": "merger"}, ErrorCode.UNSUPPORTED_ACTION),
        ({"kind": "spinoff"}, ErrorCode.UNSUPPORTED_ACTION),
        ({"kind": "delisting"}, ErrorCode.UNSUPPORTED_ACTION),
        ({"kind": "special_dividend"}, ErrorCode.UNSUPPORTED_ACTION),
        ({"currency": "EUR"}, ErrorCode.UNSUPPORTED_ACTION),
    ],
)
def test_event_temporal_revision_and_terms_fail_closed(changes, code):
    reject(code, lambda: normalize_record(CorporateActionRecord, action(**changes)))


def test_revision_and_cancellation_are_preserved_without_replaying_events():
    event = normalize_record(
        CorporateActionRecord, action(event_version=2, revision_of=1, cancelled=True)
    )
    assert event.event_version == 2 and event.revision_of == 1 and event.cancelled


def test_dividend_date_is_never_treated_as_credit_evidence():
    event = normalize_record(CorporateActionRecord, dividend())
    assert event.payable_date == date(2024, 6, 17)
    assert event.credit_evidence is None and event.credited_net is None
    reject(
        ErrorCode.CREDIT_EVIDENCE,
        lambda: normalize_record(CorporateActionRecord, credit(credit_evidence=None)),
    )


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"split_ratio": 2}, ErrorCode.ACTION_TERMS),
        ({"credited_net": 20}, ErrorCode.ACTION_TERMS),
        ({"dividend_per_share": None}, ErrorCode.ACTION_TERMS),
        ({"ex_date": None}, ErrorCode.ACTION_TERMS),
        ({"ex_date": "2024-06-11"}, ErrorCode.ACTION_TERMS),
        ({"payable_date": "2024-06-09"}, ErrorCode.TIME_ORDER),
        ({"record_date": "2024-06-18"}, ErrorCode.TIME_ORDER),
    ],
)
def test_bad_dividend_terms_are_rejected(changes, code):
    reject(code, lambda: normalize_record(CorporateActionRecord, dividend(**changes)))


@pytest.mark.parametrize("evidence", ["broker_confirmation", "simulated_assumption"])
@pytest.mark.parametrize("net", [0, 20])
def test_actual_and_simulated_credit_evidence_are_explicit(evidence, net):
    event = normalize_record(
        CorporateActionRecord, credit(credit_evidence=evidence, credited_net=net)
    )
    assert event.credit_evidence.value == evidence
    assert event.parent_event_id == "dividend-1" and event.credited_net == net


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"split_ratio": 2}, ErrorCode.ACTION_TERMS),
        ({"payable_date": "2024-06-17"}, ErrorCode.ACTION_TERMS),
        ({"credit_date": "2024-06-18"}, ErrorCode.ACTION_TERMS),
        ({"credited_net": None}, ErrorCode.ACTION_TERMS),
        ({"credited_net": -1}, ErrorCode.INVALID_VALUE),
        ({"parent_event_id": None}, ErrorCode.INVALID_VALUE),
        ({"credit_evidence_ref": None}, ErrorCode.CREDIT_EVIDENCE),
        ({"credit_evidence": "payable_date"}, ErrorCode.INVALID_VALUE),
    ],
)
def test_credit_needs_qualified_terms_and_evidence(changes, code):
    reject(code, lambda: normalize_record(CorporateActionRecord, credit(**changes)))


def test_direct_construction_cannot_bypass_contract_checks():
    raw = normalize_record(RawBar, bar())
    reject(ErrorCode.INVALID_VALUE, lambda: replace(raw, price_basis="raw"))
    reject(ErrorCode.INVALID_OHLCV, lambda: replace(raw, low=102))
    reject(ErrorCode.NAIVE_TIMESTAMP, lambda: replace(raw, available_at=datetime(2024, 1, 1)))
    assert raw.price_basis is PriceBasis.RAW
    assert raw.share_unit is ShareUnit.AS_TRADED
    assert raw.volume_unit is VolumeUnit.AS_TRADED_SHARES
    assert normalize_record(SecurityIdentity, identity()).transition is IdentityTransition.LISTING
    assert (
        normalize_record(CorporateActionRecord, credit()).credit_evidence
        is CreditEvidence.BROKER_CONFIRMATION
    )
