"""Historical facts, historical knowledge and present ingestion have distinct clocks."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from .errors import (
    SCHEMA_VERSION,
    ErrorCode,
    currency,
    day,
    enum_value,
    fail,
    number,
    schema,
    text,
    timestamp,
    version,
)
from .provenance import SourceProvenance, source


class ActionKind(StrEnum):
    SPLIT = "split"
    CASH_DIVIDEND = "cash_dividend"
    CASH_CREDIT = "cash_credit"
    MERGER = "merger"
    SPINOFF = "spinoff"
    DELISTING = "delisting"
    SPECIAL_DIVIDEND = "special_dividend"


class CreditEvidence(StrEnum):
    BROKER_CONFIRMATION = "broker_confirmation"
    SIMULATED_ASSUMPTION = "simulated_assumption"


@dataclass(frozen=True, slots=True, kw_only=True)
class CorporateActionRecord:
    security_id: str
    event_id: str
    kind: ActionKind
    effective_date: date
    historically_known_at: datetime | None
    knowledge_evidence: str | None
    event_version: int
    source: SourceProvenance
    currency: str
    revision_of: int | None = None
    cancelled: bool = False
    split_ratio: float | None = None  # new shares / old shares, including reverse splits
    dividend_per_share: float | None = None
    ex_date: date | None = None
    record_date: date | None = None
    payable_date: date | None = None
    credit_date: date | None = None
    parent_event_id: str | None = None
    credited_net: float | None = None
    credit_evidence: CreditEvidence | None = None
    credit_evidence_ref: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        schema(self.schema_version)
        source(self.source)
        text(self.security_id, "security_id")
        text(self.event_id, "event_id")
        currency(self.currency)
        enum_value(self.kind, ActionKind, "kind")
        if self.kind not in (ActionKind.SPLIT, ActionKind.CASH_DIVIDEND, ActionKind.CASH_CREDIT):
            fail(ErrorCode.UNSUPPORTED_ACTION, "kind", "Complex corporate actions are unsupported")
        if self.currency != "USD":
            fail(
                ErrorCode.UNSUPPORTED_ACTION, "currency", "Only ordinary USD actions are supported"
            )
        version(self.event_version, "event_version")
        if self.revision_of is not None:
            version(self.revision_of, "revision_of")
        if (self.event_version == 1 and self.revision_of is not None) or (
            self.event_version > 1 and self.revision_of != self.event_version - 1
        ):
            fail(
                ErrorCode.ACTION_TERMS,
                "revision_of",
                "Revision must identify its preceding version",
            )
        if type(self.cancelled) is not bool:
            fail(ErrorCode.INVALID_TYPE, "cancelled", "Expected boolean")
        for field in ("effective_date", "ex_date", "record_date", "payable_date", "credit_date"):
            value = getattr(self, field)
            if value is not None:
                day(value, field)
        # effective_date is required, including direct construction with None.
        day(self.effective_date, "effective_date")
        if self.historically_known_at is not None:
            known = timestamp(self.historically_known_at, "historically_known_at")
            object.__setattr__(self, "historically_known_at", known)
            if known > self.source.ingested_at:
                fail(
                    ErrorCode.TIME_ORDER,
                    "historically_known_at",
                    "Knowledge cannot follow this ingestion",
                )
            if self.knowledge_evidence is None:
                fail(
                    ErrorCode.KNOWLEDGE_EVIDENCE,
                    "knowledge_evidence",
                    "Historical knowledge needs a source reference",
                )
            text(self.knowledge_evidence, "knowledge_evidence")
        elif self.knowledge_evidence is not None:
            fail(
                ErrorCode.KNOWLEDGE_EVIDENCE,
                "historically_known_at",
                "Unknown public time must remain explicitly unknown",
            )
        for field in ("split_ratio", "dividend_per_share", "credited_net"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(
                    self, field, number(value, field, positive=field == "split_ratio")
                )
        dividend_fields = (
            self.dividend_per_share,
            self.ex_date,
            self.record_date,
            self.payable_date,
        )
        credit_fields = (
            self.credit_date,
            self.parent_event_id,
            self.credited_net,
            self.credit_evidence,
            self.credit_evidence_ref,
        )
        if self.kind is ActionKind.SPLIT:
            if self.split_ratio is None or any(
                v is not None for v in (*dividend_fields, *credit_fields)
            ):
                fail(
                    ErrorCode.ACTION_TERMS,
                    "split_ratio",
                    "Split requires only a positive new/old ratio",
                )
        elif self.kind is ActionKind.CASH_DIVIDEND:
            if self.split_ratio is not None or any(v is not None for v in credit_fields):
                fail(
                    ErrorCode.ACTION_TERMS, "kind", "Dividend cannot contain split or credit terms"
                )
            if self.dividend_per_share is None or self.ex_date != self.effective_date:
                fail(
                    ErrorCode.ACTION_TERMS,
                    "ex_date",
                    "Dividend requires amount and effective ex-date",
                )
            if self.payable_date is not None and (
                self.payable_date < self.effective_date
                or (self.record_date is not None and self.record_date > self.payable_date)
            ):
                fail(
                    ErrorCode.TIME_ORDER,
                    "payable_date",
                    "Payable date precedes dividend qualification",
                )
        else:
            if self.split_ratio is not None or any(v is not None for v in dividend_fields):
                fail(
                    ErrorCode.ACTION_TERMS, "kind", "Credit cannot contain split or dividend terms"
                )
            if self.credit_date != self.effective_date or self.credited_net is None:
                fail(
                    ErrorCode.ACTION_TERMS,
                    "credit_date",
                    "Credit requires date and actual/simulated net amount",
                )
            text(self.parent_event_id, "parent_event_id")
            if self.credit_evidence is None or self.credit_evidence_ref is None:
                fail(
                    ErrorCode.CREDIT_EVIDENCE,
                    "credit_evidence",
                    "Credit needs explicit broker evidence or simulation assumption",
                )
            enum_value(self.credit_evidence, CreditEvidence, "credit_evidence")
            text(self.credit_evidence_ref, "credit_evidence_ref")
            if self.credit_evidence is CreditEvidence.BROKER_CONFIRMATION:
                zone = ZoneInfo(self.source.source_timezone)
                if self.source.ingested_at.astimezone(zone).date() < self.effective_date or (
                    self.historically_known_at is not None
                    and self.historically_known_at.astimezone(zone).date() < self.effective_date
                ):
                    fail(
                        ErrorCode.TIME_ORDER,
                        "credit_date",
                        "Broker credit evidence cannot precede the credited date",
                    )
