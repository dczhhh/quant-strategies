"""Strict offline normalization of canonical mappings, without data repair or I/O."""

from collections.abc import Iterable, Mapping
from dataclasses import MISSING, fields
from datetime import date, datetime
from enum import StrEnum
from typing import cast

from .contracts.corporate_actions import ActionKind, CorporateActionRecord, CreditEvidence
from .contracts.errors import ContractError, ErrorCode, fail
from .contracts.identities import IdentityMap, IdentityTransition, SecurityIdentity
from .contracts.market_data import PriceBasis, RawBar, ShareUnit, SignalBar, VolumeUnit
from .contracts.provenance import SourceProvenance

NORMALIZER_VERSION = "research_normalizer_v2"

_RECORD_TYPES = (SourceProvenance, SecurityIdentity, RawBar, SignalBar, CorporateActionRecord)
_TIMES = frozenset(
    {
        "bar_start_at",
        "bar_end_at",
        "available_at",
        "ingested_at",
        "historically_known_at",
        "effective_from",
        "effective_until",
    }
)
_DAYS = frozenset({"effective_date", "ex_date", "record_date", "payable_date", "credit_date"})
_ENUMS: dict[str, type[StrEnum]] = {
    "kind": ActionKind,
    "credit_evidence": CreditEvidence,
    "transition": IdentityTransition,
    "price_basis": PriceBasis,
    "share_unit": ShareUnit,
    "volume_unit": VolumeUnit,
}


def normalize_record[
    Record: SourceProvenance | SecurityIdentity | RawBar | SignalBar | CorporateActionRecord
](record_type: type[Record], record: Mapping[str, object]) -> Record:
    """Normalize ISO dates/aware timestamps and enums; preserve source values and units."""
    if record_type not in _RECORD_TYPES or not isinstance(record, Mapping):
        fail(ErrorCode.INVALID_TYPE, "record", "Expected supported contract type and mapping")
    if any(not isinstance(key, str) for key in record):
        fail(ErrorCode.INVALID_TYPE, "record", "Field names must be strings")
    definitions = {field.name: field for field in fields(record_type)}
    unknown = sorted(record.keys() - definitions.keys())
    if unknown:
        fail(ErrorCode.UNKNOWN_FIELD, unknown[0], "Unexpected canonical field")
    missing = sorted(
        name
        for name, field in definitions.items()
        if field.default is MISSING and field.default_factory is MISSING and name not in record
    )
    if missing:
        fail(ErrorCode.MISSING_FIELD, missing[0], "Required canonical field is missing")
    values = dict(record)
    for name in sorted(record):
        value = record[name]
        if value is None:
            continue
        try:
            if name in _TIMES and isinstance(value, str):
                values[name] = datetime.fromisoformat(value)
            elif name in _DAYS and isinstance(value, str):
                values[name] = date.fromisoformat(value)
            elif name in _ENUMS:
                values[name] = _ENUMS[name](value)
            elif name == "source" and isinstance(value, Mapping):
                values[name] = normalize_record(SourceProvenance, cast(Mapping[str, object], value))
        except ContractError:
            raise
        except (TypeError, ValueError):
            fail(ErrorCode.INVALID_VALUE, name, "Malformed canonical date, timestamp or enum")
    return record_type(**values)


def normalize_identities(records: Iterable[Mapping[str, object]]) -> IdentityMap:
    return IdentityMap(tuple(normalize_record(SecurityIdentity, record) for record in records))


def _bars(
    record_type: type[RawBar] | type[SignalBar],
    records: Iterable[Mapping[str, object]],
    identities: IdentityMap,
) -> tuple[RawBar | SignalBar, ...]:
    if not isinstance(identities, IdentityMap):
        fail(ErrorCode.INVALID_TYPE, "identities", "Expected IdentityMap")
    bars = sorted(
        (normalize_record(record_type, record) for record in records),
        key=lambda bar: (
            bar.security_id,
            bar.venue,
            bar.product_id if record_type is SignalBar else "raw",
            bar.bar_start_at,
            bar.bar_end_at,
        ),
    )
    seen: set[tuple] = set()
    source_ids: set[tuple] = set()
    partitions: dict[tuple[str, str, str, str], str] = {}
    previous: dict[tuple, RawBar | SignalBar] = {}
    for bar in bars:
        identity = identities.resolve_effective(bar.symbol, bar.venue, bar.bar_start_at)
        if (
            identity.security_id != bar.security_id
            or identity.currency != bar.currency
            or (identity.effective_until is not None and bar.bar_end_at > identity.effective_until)
        ):
            fail(
                ErrorCode.IDENTITY_CONFLICT,
                "security_id",
                "Bar disagrees with effective identity or crosses its boundary",
            )
        # Raw execution may not contain competing products for the same interval.
        product = bar.product_id if record_type is SignalBar else "raw"
        group = (bar.security_id, bar.venue, product)
        key = (*group, bar.bar_start_at, bar.bar_end_at)
        partition = (
            bar.source.provider,
            bar.source.dataset_id,
            bar.source.dataset_revision,
            bar.source.source_partition_id,
        )
        reference = partitions.setdefault(partition, bar.source.source_partition_ref)
        if reference != bar.source.source_partition_ref:
            fail(
                ErrorCode.PROVENANCE_CONFLICT,
                "source_partition_ref",
                "One source partition has conflicting references",
            )
        origin = (
            *partition,
            bar.source.record_id,
            bar.source.record_version,
        )
        if key in seen or origin in source_ids:
            fail(ErrorCode.DUPLICATE_BAR, "bar_start_at", "Duplicate bar or original source record")
        seen.add(key)
        source_ids.add(origin)
        earlier = previous.get(group)
        if earlier is not None and bar.bar_start_at < earlier.bar_end_at:
            fail(ErrorCode.OVERLAPPING_BAR, "bar_start_at", "Bars overlap within a data product")
        previous[group] = bar
    # Return chronological order independent of record input order or timezone notation.
    return tuple(
        sorted(
            bars,
            key=lambda bar: (
                bar.bar_start_at,
                bar.security_id,
                bar.venue,
                bar.product_id,
                bar.bar_end_at,
            ),
        )
    )


def normalize_raw_bars(
    records: Iterable[Mapping[str, object]],
    identities: IdentityMap,
) -> tuple[RawBar, ...]:
    return tuple(bar for bar in _bars(RawBar, records, identities) if isinstance(bar, RawBar))


def normalize_signal_bars(
    records: Iterable[Mapping[str, object]],
    identities: IdentityMap,
) -> tuple[SignalBar, ...]:
    return tuple(bar for bar in _bars(SignalBar, records, identities) if isinstance(bar, SignalBar))
