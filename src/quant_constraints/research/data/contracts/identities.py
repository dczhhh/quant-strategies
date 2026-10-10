"""Effective identity facts; these are not historical-knowledge/PIT lookups."""

from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from .errors import SCHEMA_VERSION, ErrorCode, currency, enum_value, fail, schema, text, timestamp
from .provenance import SourceProvenance, source


class IdentityTransition(StrEnum):
    LISTING = "listing"
    RENAME = "rename"
    MERGER = "merger"
    DELISTING = "delisting"


@dataclass(frozen=True, slots=True, kw_only=True)
class SecurityIdentity:
    security_id: str
    symbol: str
    venue: str
    currency: str
    effective_from: datetime
    effective_until: datetime | None
    transition: IdentityTransition
    source: SourceProvenance
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        schema(self.schema_version)
        source(self.source)
        for name in ("security_id", "symbol", "venue"):
            text(getattr(self, name), name)
        currency(self.currency)
        enum_value(self.transition, IdentityTransition, "transition")
        if self.transition not in (IdentityTransition.LISTING, IdentityTransition.RENAME):
            fail(
                ErrorCode.UNSUPPORTED_IDENTITY,
                "transition",
                "Merger and delisting mapping are unsupported",
            )
        object.__setattr__(self, "effective_from", timestamp(self.effective_from, "effective_from"))
        if self.effective_until is not None:
            object.__setattr__(
                self, "effective_until", timestamp(self.effective_until, "effective_until")
            )
            if self.effective_until <= self.effective_from:
                fail(ErrorCode.TIME_ORDER, "effective_until", "Identity interval must be nonempty")


@dataclass(frozen=True, slots=True)
class IdentityMap:
    entries: tuple[SecurityIdentity, ...]
    _index: Mapping[tuple[str, str], tuple[tuple[datetime, ...], tuple[SecurityIdentity, ...]]] = (
        field(init=False, repr=False, compare=False)
    )

    def __post_init__(self):
        if not isinstance(self.entries, tuple) or not all(
            isinstance(e, SecurityIdentity) for e in self.entries
        ):
            fail(ErrorCode.INVALID_TYPE, "entries", "Expected immutable tuple of SecurityIdentity")
        entries = tuple(
            sorted(self.entries, key=lambda e: (e.venue, e.symbol, e.effective_from, e.security_id))
        )
        by_symbol: dict[tuple[str, str], list[SecurityIdentity]] = {}
        by_security: dict[str, list[SecurityIdentity]] = {}
        for entry in entries:
            by_symbol.setdefault((entry.symbol, entry.venue), []).append(entry)
            by_security.setdefault(entry.security_id, []).append(entry)
        for group in by_symbol.values():
            if len({e.security_id for e in group}) > 1:
                fail(
                    ErrorCode.UNSUPPORTED_IDENTITY,
                    "symbol",
                    "Ticker reuse across different securities is unsupported",
                )
        for group in (*by_symbol.values(), *by_security.values()):
            ordered = sorted(group, key=lambda e: e.effective_from)
            for previous, current in zip(ordered, ordered[1:]):
                if (
                    previous.effective_until is None
                    or previous.effective_until > current.effective_from
                ):
                    fail(
                        ErrorCode.IDENTITY_CONFLICT, "effective_from", "Identity intervals overlap"
                    )
        for group in by_security.values():
            ordered = sorted(group, key=lambda e: e.effective_from)
            for index, entry in enumerate(ordered):
                if entry.transition is IdentityTransition.RENAME and (
                    index == 0
                    or ordered[index - 1].effective_until != entry.effective_from
                    or ordered[index - 1].symbol == entry.symbol
                    or ordered[index - 1].venue != entry.venue
                    or ordered[index - 1].currency != entry.currency
                ):
                    fail(
                        ErrorCode.IDENTITY_CONFLICT,
                        "transition",
                        "Rename needs a contiguous predecessor of the same security, venue and currency",
                    )
                if (
                    index > 0
                    and ordered[index - 1].symbol != entry.symbol
                    and entry.transition is not IdentityTransition.RENAME
                ):
                    fail(
                        ErrorCode.IDENTITY_CONFLICT,
                        "transition",
                        "Symbol change requires an explicit rename",
                    )
        object.__setattr__(self, "entries", entries)
        object.__setattr__(
            self,
            "_index",
            MappingProxyType(
                {
                    key: (tuple(entry.effective_from for entry in group), tuple(group))
                    for key, group in by_symbol.items()
                }
            ),
        )

    def __reduce__(self):
        # Only canonical entries travel through a checkpoint; rebuild the cache.
        return type(self), (self.entries,)

    def resolve_effective(self, symbol: str, venue: str, at: datetime) -> SecurityIdentity:
        at = timestamp(at, "at")
        group = self._index.get((symbol, venue))
        if group is not None:
            index = bisect_right(group[0], at) - 1
            if index >= 0:
                entry = group[1][index]
                end = entry.effective_until
                if end is None or at < end:
                    return entry
        fail(ErrorCode.IDENTITY_MISSING, "symbol", "No identity covers this effective timestamp")
