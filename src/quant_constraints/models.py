"""Immutable input snapshots and auditable decisions; no signal generation."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Action(StrEnum):
    ALLOW = "ALLOW"
    REJECT = "REJECT"
    DEFER = "DEFER"
    RESIZE = "RESIZE"


class Kind(StrEnum):
    ENTRY = "entry"
    ADD = "add"
    REDUCE = "reduce"
    RISK = "risk"
    REBALANCE = "rebalance"


@dataclass(frozen=True)
class Intent:
    asset: str
    quantity: float  # signed shares; fractional shares allowed
    order_id: str = "external"
    kind: Kind = Kind.ENTRY
    order_type: str = "market"
    target_weight: float | None = None


@dataclass(frozen=True)
class Holding:
    quantity: float
    price: float
    sector: str | None = None
    opened_at: datetime | None = None


@dataclass(frozen=True)
class State:
    settled_cash: float
    equity: float
    holdings: dict[str, Holding] = field(default_factory=dict)
    reserved_cash: float = 0.0  # excludes the intent being checked
    pending_buys: dict[str, float] = field(default_factory=dict)  # notional, excludes intent
    pending_sells: dict[str, float] = field(default_factory=dict)  # shares, excludes intent
    entry_times: tuple[datetime, ...] = ()
    anchor: datetime | None = None
    drawdown: float = 0.0
    pending_sectors: dict[str, str | None] = field(default_factory=dict)
    missing_marks: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MarketContext:
    asof: datetime
    price: float
    commission: float = 0.0
    phase: str = "submission"
    data_frequency: str = "1m"
    sector: str | None = None
    instrument: str = "unknown"  # equity or plain_sector_etf; explicit metadata
    spread: float | None = None  # relative bid/ask spread
    rvol: float | None = None  # historical same-time relative volume
    volume: float | None = None
    liquidity_available_at: datetime | None = None
    vix: float | None = None
    vix_available_at: datetime | None = None

    def __post_init__(self):
        aware(self.asof)
        for timestamp in (self.liquidity_available_at, self.vix_available_at):
            if timestamp is not None:
                aware(timestamp)


@dataclass(frozen=True)
class Decision:
    action: Action
    code: str
    reason: str = ""
    quantity: float | None = None
    data: dict[str, object] = field(default_factory=dict)


ALLOW = Decision(Action.ALLOW, "allowed")


@dataclass(frozen=True)
class Audit:
    timestamp: datetime
    asset: str
    order_id: str
    order_kind: str
    order_type: str
    phase: str
    available_settled_cash: float
    required_funds: float
    decision: Decision


@dataclass(frozen=True)
class RiskRequest:
    asset: str
    quantity: float  # positive reduction shares
    reason: str


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Constraint timestamps must be timezone-aware")
    return value
