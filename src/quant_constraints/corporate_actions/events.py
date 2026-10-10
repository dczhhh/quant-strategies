"""Source-authoritative event terms and availability, including credit confirmations."""

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from ..calendar import NY
from ..models import aware


@dataclass(frozen=True)
class CorporateAction:
    security_id: str
    event_id: str
    kind: str  # SPLIT, CASH_DIVIDEND, CASH_CREDIT; other kinds fail closed when exposed
    effective_date: date
    available_at: datetime
    source: str
    version: int = 1
    currency: str = "USD"
    split_ratio: float | None = None  # new shares / old shares
    dividend_per_share: float | None = None
    record_date: date | None = None
    payable_date: date | None = None  # metadata, NOT proof of broker credit
    parent_event_id: str | None = None  # CASH_CREDIT identifies its entitlement
    credited_net: float | None = None  # actual net dollars for this account, if known
    withholding_rate: float | None = None  # source/account-specific, otherwise configured scenario
    fee_per_share: float = 0.0
    fractional_policy: str = "retain"
    quantity_precision: int = 12
    dividend_basis: str = "post_split"
    terms: str = "ordinary"
    cancelled: bool = False
    tax_source_country: str | None = None
    tax_source: str | None = None
    tax_source_known_at: datetime | None = None
    distribution_type: str | None = None
    withholding_source: str | None = None
    withholding_known_at: datetime | None = None

    def __post_init__(self):
        aware(self.available_at)
        for stamp in (self.tax_source_known_at, self.withholding_known_at):
            if stamp is not None:
                aware(stamp)
        if self.tax_source_country is not None and (
            len(self.tax_source_country) != 2
            or not self.tax_source_country.isascii()
            or not self.tax_source_country.isupper()
            or not self.tax_source_country.isalpha()
        ):
            raise ValueError("Tax source country must be a two-letter uppercase code")
        if any(
            not isinstance(x, str) or not x.strip()
            for x in (self.security_id, self.event_id, self.kind, self.source, self.currency)
        ):
            raise ValueError("Corporate actions require stable IDs, kind, currency and source")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("Invalid corporate action version")
        for name in (
            "split_ratio",
            "dividend_per_share",
            "credited_net",
            "withholding_rate",
            "fee_per_share",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"Invalid corporate action {name}")
        if self.kind == "SPLIT" and (self.split_ratio is None or self.split_ratio <= 0):
            raise ValueError("A split requires a positive new/old ratio")
        if self.kind == "CASH_DIVIDEND" and self.dividend_per_share is None:
            raise ValueError("A dividend requires a per-share amount")
        if self.kind == "CASH_CREDIT" and not self.parent_event_id:
            raise ValueError("A credit requires its parent entitlement ID")
        if (
            self.kind == "CASH_CREDIT"
            and self.available_at.astimezone(NY).date() < self.effective_date
        ):
            raise ValueError("A credit confirmation cannot precede actual credit date")
        if self.withholding_rate is not None and self.withholding_rate > 1:
            raise ValueError("Withholding must be a fraction")
        if type(self.quantity_precision) is not int or not 0 <= self.quantity_precision <= 12:
            raise ValueError("Quantity precision must be between 0 and 12")
        if type(self.cancelled) is not bool:
            raise ValueError("cancelled must be boolean")
        for name in ("effective_date", "record_date", "payable_date"):
            value = getattr(self, name)
            if value is not None and type(value) is not date:
                raise ValueError(f"{name} must be a date")

    @property
    def key(self) -> tuple[str, str]:
        return self.security_id, self.event_id

    @property
    def economics(self) -> tuple:
        """Financial identity excludes harmless source/version metadata revisions."""
        return (
            self.kind,
            self.effective_date,
            self.currency,
            self.split_ratio,
            self.dividend_per_share,
            self.parent_event_id,
            self.credited_net,
            self.withholding_rate,
            self.fee_per_share,
            self.fractional_policy,
            self.quantity_precision,
            self.dividend_basis,
            self.terms,
            self.cancelled,
            self.tax_source_country,
            self.tax_source,
            self.tax_source_known_at,
            self.distribution_type,
            self.withholding_source,
            self.withholding_known_at,
        )


@dataclass(frozen=True)
class CorporateActionCoverage:
    security_id: str
    available_at: datetime
    covered_from: date
    covered_until: date
    source: str
    point_in_time: bool = True
    missing: bool = False

    def __post_init__(self):
        aware(self.available_at)
        if not self.security_id or not self.source or self.covered_from > self.covered_until:
            raise ValueError("Invalid corporate action coverage")
        if type(self.point_in_time) is not bool or type(self.missing) is not bool:
            raise ValueError("Coverage flags must be boolean")


class CorporateActionProvider(Protocol):
    def snapshot(
        self, security_id: str, asof: datetime
    ) -> tuple[CorporateActionCoverage | None, tuple[CorporateAction, ...]]: ...


class InMemoryCorporateActionProvider:
    """Deterministic fixture/adapter: latest visible revision, including tombstones."""

    def __init__(
        self,
        events: tuple[CorporateAction, ...] = (),
        coverage: tuple[CorporateActionCoverage, ...] = (),
    ):
        self.events, self.coverage = events, coverage

    def snapshot(self, security_id: str, asof: datetime):
        aware(asof)
        known = [
            c for c in self.coverage if c.security_id == security_id and c.available_at <= asof
        ]
        coverage = max(known, key=lambda c: c.available_at) if known else None
        latest = {}
        for event in sorted(self.events, key=lambda e: (e.available_at, e.version)):
            if event.security_id == security_id and event.available_at <= asof:
                previous = latest.get(event.event_id)
                if (
                    previous
                    and (previous.available_at, previous.version)
                    == (event.available_at, event.version)
                    and previous.economics != event.economics
                ):
                    raise ValueError("Conflicting corporate action revision")
                latest[event.event_id] = event
        return coverage, tuple(latest.values())
