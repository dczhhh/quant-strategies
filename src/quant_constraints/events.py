"""Revision-aware earnings calendar. Future schedules require historical availability."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .models import aware


@dataclass(frozen=True)
class EarningsEvent:
    asset: str
    event_id: str
    announcement_at: datetime
    available_at: datetime
    timing: str  # BMO, AMC, DURING, UNKNOWN
    source: str
    missing: bool = False
    cancelled: bool = False

    def __post_init__(self):
        aware(self.announcement_at)
        aware(self.available_at)
        if self.timing not in {"BMO", "AMC", "DURING", "UNKNOWN"} or not self.source:
            raise ValueError("Invalid earnings timing or source")


@dataclass(frozen=True)
class EarningsCoverage:
    asset: str
    available_at: datetime
    covered_until: datetime
    source: str
    missing: bool = False

    def __post_init__(self):
        aware(self.available_at)
        aware(self.covered_until)
        if not self.source:
            raise ValueError("Coverage source is required")


class EarningsProvider(Protocol):
    def snapshot(
        self, asset: str, asof: datetime
    ) -> tuple[EarningsCoverage | None, tuple[EarningsEvent, ...]]: ...


class InMemoryEarningsProvider:
    def __init__(
        self, events: tuple[EarningsEvent, ...] = (), coverage: tuple[EarningsCoverage, ...] = ()
    ):
        self.events = events
        self.coverage = coverage

    def snapshot(
        self, asset: str, asof: datetime
    ) -> tuple[EarningsCoverage | None, tuple[EarningsEvent, ...]]:
        aware(asof)
        known = [c for c in self.coverage if c.asset == asset and c.available_at <= asof]
        coverage = max(known, key=lambda c: c.available_at) if known else None
        latest: dict[str, EarningsEvent] = {}
        for event in sorted(self.events, key=lambda event: event.available_at):
            if event.asset == asset and event.available_at <= asof:
                latest[event.event_id] = event
        return coverage, tuple(event for event in latest.values() if not event.cancelled)
