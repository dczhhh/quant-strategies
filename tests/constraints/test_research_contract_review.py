"""Regression and structural performance gates for the first 5A review."""

import copy
import pickle
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest
from test_research_data_contracts import bar, identity, provenance, reject, signal

import quant_constraints.research.data.contracts.identities as identity_module
from quant_constraints.research.data.contracts import (
    ContractError,
    ErrorCode,
    IdentityMap,
    RawBar,
    SecurityIdentity,
    SignalBar,
    SourceProvenance,
    validate_price_risk_input,
)
from quant_constraints.research.data.contracts.errors import timestamp
from quant_constraints.research.data.normalize import (
    normalize_identities,
    normalize_raw_bars,
    normalize_record,
    normalize_signal_bars,
)


def linear_lookup(mapping, symbol, venue, at):
    at = timestamp(at, "at")
    for entry in mapping.entries:
        if (
            entry.symbol == symbol
            and entry.venue == venue
            and entry.effective_from <= at
            and (entry.effective_until is None or at < entry.effective_until)
        ):
            return entry
    raise ContractError(
        ErrorCode.IDENTITY_MISSING, "symbol", "No identity covers this effective timestamp"
    )


class LinearIdentityMap(IdentityMap):
    def resolve_effective(self, symbol, venue, at):
        return linear_lookup(self, symbol, venue, at)


@pytest.mark.parametrize("assets", [1, 10, 100, 1000])
def test_queries_read_only_the_selected_candidate(assets, monkeypatch):
    mapping = normalize_identities(
        [identity(symbol=f"S{i:04}", security_id=f"ID{i:04}") for i in range(assets)]
    )
    reads = []
    original = SecurityIdentity.__getattribute__

    def count(instance, name):
        if name in ("symbol", "venue", "effective_from", "effective_until"):
            reads.append((original(instance, "security_id"), name))
        return original(instance, name)

    with monkeypatch.context() as patch:
        patch.setattr(SecurityIdentity, "__getattribute__", count)
        for _ in range(100):
            mapping.resolve_effective(f"S{assets - 1:04}", "XNAS", datetime(2024, 6, 7, tzinfo=UTC))
    assert len(reads) == 100
    assert set(reads) == {(f"ID{assets - 1:04}", "effective_until")}


def test_index_is_deeply_read_only_and_rebuilt_on_copy_or_pickle():
    mapping = normalize_identities([identity()])
    assert isinstance(mapping._index, MappingProxyType)
    key = ("AAA", "XNAS")
    with pytest.raises(TypeError):
        mapping._index[key] = ()
    with pytest.raises(TypeError):
        mapping._index[key][0][0] = datetime(2024, 1, 1, tzinfo=UTC)
    with pytest.raises(FrozenInstanceError):
        mapping._index = {}
    for restored in (copy.deepcopy(mapping), pickle.loads(pickle.dumps(mapping))):
        assert restored == mapping
        assert isinstance(restored._index, MappingProxyType)
        assert (
            restored.resolve_effective("AAA", "XNAS", datetime(2024, 6, 7, tzinfo=UTC))
            == mapping.entries[0]
        )


@pytest.mark.parametrize("day", [0, 1, 2, 3, 4, 5, 6, 7])
def test_binary_search_matches_linear_across_gaps_and_half_open_boundaries(day):
    start = datetime(2024, 1, 1, tzinfo=UTC)
    records = [
        identity(
            effective_from=start + timedelta(days=i), effective_until=start + timedelta(days=i + 1)
        )
        for i in (1, 3, 5)
    ]
    mapping = normalize_identities(reversed(records))
    at = start + timedelta(days=day)
    for symbol in ("AAA", "UNKNOWN"):
        try:
            expected = linear_lookup(mapping, symbol, "XNAS", at)
        except ContractError as error:
            actual = reject(
                error.code, lambda symbol=symbol: mapping.resolve_effective(symbol, "XNAS", at)
            )
            assert actual.as_dict() == error.as_dict()
        else:
            assert mapping.resolve_effective(symbol, "XNAS", at) == expected


@pytest.mark.parametrize("record_type,factory", [(RawBar, bar), (SignalBar, signal)])
def test_risk_symbol_disagreement_rejects_even_with_matching_stable_id(record_type, factory):
    execution = normalize_record(RawBar, bar())
    risk = normalize_record(record_type, factory(symbol="WRONG"))
    reject(ErrorCode.UNIT_MISMATCH, lambda: validate_price_risk_input(execution, risk))


def test_rename_products_are_consistent_on_each_side_and_match_linear_normalization():
    boundary = "2024-06-07T13:31:00Z"
    mapping = normalize_identities(
        [
            identity(symbol="NEW", effective_from=boundary, transition="rename"),
            identity(effective_until=boundary),
        ]
    )
    before = bar()
    after = bar(
        symbol="NEW",
        bar_start_at=boundary,
        bar_end_at="2024-06-07T13:32:00Z",
        available_at="2024-06-07T13:32:01Z",
        source=provenance(record_id="bar-2"),
    )
    records = [after, before]
    raw = normalize_raw_bars(records, mapping)
    baseline = LinearIdentityMap(mapping.entries)
    assert raw == normalize_raw_bars(list(reversed(records)), baseline)
    risk_records = [
        record | {"product_id": "signal-minutes", "raw_product_id": "raw-minutes"}
        for record in records
    ]
    risks = normalize_signal_bars(risk_records, mapping)
    assert risks == normalize_signal_bars(list(reversed(risk_records)), baseline)
    for execution, risk in zip(raw, risks, strict=True):
        validate_price_risk_input(execution, risk)
        assert execution.symbol == risk.symbol


@pytest.mark.parametrize("partitioned", [False, True])
def test_record_ids_require_an_explicit_source_namespace(partitioned):
    mapping = normalize_identities([identity(), identity(symbol="BBB", security_id="B")])
    first = bar(
        source=provenance(
            record_id="1", source_partition_id="file-A", source_partition_ref="fixture://file-A"
        )
    )
    second = bar(
        symbol="BBB",
        security_id="B",
        source=provenance(
            record_id="1",
            source_partition_id="file-B" if partitioned else "file-A",
            source_partition_ref="fixture://file-B" if partitioned else "fixture://file-A",
        ),
    )
    if partitioned:
        assert len(normalize_raw_bars([second, first], mapping)) == 2
    else:
        reject(ErrorCode.DUPLICATE_BAR, lambda: normalize_raw_bars([second, first], mapping))


@pytest.mark.parametrize("field", ["source_partition_id", "source_partition_ref"])
def test_source_namespace_cannot_be_missing_blank_or_null(field):
    record = provenance()
    record.pop(field, None)
    reject(ErrorCode.MISSING_FIELD, lambda: normalize_record(SourceProvenance, record))
    for value in (None, "", " "):
        reject(
            ErrorCode.INVALID_VALUE,
            lambda value=value: normalize_record(SourceProvenance, provenance(**{field: value})),
        )


def test_namespace_does_not_hide_duplicate_intervals_or_conflicting_references():
    mapping = normalize_identities([identity()])
    first = bar()
    duplicate = bar(
        source=provenance(
            record_id="2",
            source_partition_id="different",
            source_partition_ref="fixture://different",
        )
    )
    reject(ErrorCode.DUPLICATE_BAR, lambda: normalize_raw_bars([first, duplicate], mapping))
    later = bar(
        bar_start_at="2024-06-07T13:31:00Z",
        bar_end_at="2024-06-07T13:32:00Z",
        available_at="2024-06-07T13:32:01Z",
        source=provenance(record_id="2", source_partition_ref="fixture://conflicting"),
    )
    reject(ErrorCode.PROVENANCE_CONFLICT, lambda: normalize_raw_bars([later, first], mapping))


def test_unknown_or_before_first_identity_does_not_read_candidates(monkeypatch):
    mapping = normalize_identities([identity()])
    original = SecurityIdentity.__getattribute__

    def prohibit(instance, name):
        if name == "effective_until":
            raise AssertionError("No candidate should be inspected")
        return original(instance, name)

    with monkeypatch.context() as patch:
        patch.setattr(SecurityIdentity, "__getattribute__", prohibit)
        reject(
            ErrorCode.IDENTITY_MISSING,
            lambda: mapping.resolve_effective("UNKNOWN", "XNAS", datetime(2024, 1, 1, tzinfo=UTC)),
        )
        reject(
            ErrorCode.IDENTITY_MISSING,
            lambda: mapping.resolve_effective("AAA", "XNAS", datetime(1999, 1, 1, tzinfo=UTC)),
        )


def test_schema_one_cannot_silently_acquire_a_namespace():
    reject(
        ErrorCode.UNSUPPORTED_SCHEMA,
        lambda: normalize_record(RawBar, bar(schema_version="research_data_v1")),
    )


def test_bisect_uses_only_the_target_sorted_interval_group(monkeypatch):
    start = datetime(2020, 1, 1, tzinfo=UTC)
    records = [
        identity(
            effective_from=start + timedelta(days=2 * i),
            effective_until=start + timedelta(days=2 * i + 1),
        )
        for i in range(1000)
    ]
    records.append(identity(symbol="UNRELATED", security_id="other"))
    mapping = normalize_identities(reversed(records))
    calls = []
    original = identity_module.bisect_right

    def spy(starts, at):
        calls.append(starts)
        return original(starts, at)

    monkeypatch.setattr(identity_module, "bisect_right", spy)
    for i in (0, 1, 500, 999):
        at = start + timedelta(days=2 * i, hours=12)
        assert mapping.resolve_effective("AAA", "XNAS", at) == linear_lookup(
            mapping, "AAA", "XNAS", at
        )
    assert len(calls) == 4
    assert all(
        starts is mapping._index[("AAA", "XNAS")][0] and len(starts) == 1000 for starts in calls
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "UNKNOWN"},
        {"security_id": "wrong"},
        {"currency": "EUR"},
        {"bar_end_at": "2024-06-07T13:31:01Z"},
    ],
)
def test_normalization_error_evidence_matches_linear_on_invalid_inputs(changes):
    mapping = normalize_identities([identity(effective_until="2024-06-07T13:31:00Z")])
    baseline = LinearIdentityMap(mapping.entries)
    records = [bar(**changes)]
    with pytest.raises(ContractError) as expected:
        normalize_raw_bars(records, baseline)
    with pytest.raises(ContractError) as actual:
        normalize_raw_bars(records, mapping)
    assert actual.value.as_dict() == expected.value.as_dict()
