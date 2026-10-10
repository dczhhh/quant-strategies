"""PIT macro schedule fixtures and read-only admission; no economic predictions."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from .calendar import NY, SessionCalendar
from .models import ALLOW, Action, Decision, Intent, MarketContext, State, aware

if TYPE_CHECKING:
    from .config import ConstraintConfig

MACRO_TYPES = frozenset({"CPI", "NFP", "PCE", "PPI", "FOMC_STATEMENT", "FOMC_PRESS", "ISM"})


@dataclass(frozen=True)
class MacroEvent:
    event_id: str
    event_type: str
    scheduled_at: datetime
    scheduled_known_at: datetime
    source: str
    revision: int = 1
    published_at: datetime | None = None
    cancelled: bool = False
    missing: bool = False

    def __post_init__(self):
        aware(self.scheduled_at)
        aware(self.scheduled_known_at)
        if self.published_at is not None:
            aware(self.published_at)
        if (
            not self.event_id.strip()
            or not self.source.strip()
            or self.event_type not in MACRO_TYPES
            or type(self.revision) is not int
            or self.revision < 1
            or type(self.cancelled) is not bool
            or type(self.missing) is not bool
        ):
            raise ValueError("Invalid macro event")


@dataclass(frozen=True)
class MacroEventCoverage:
    available_at: datetime
    covered_from: datetime
    covered_until: datetime
    source: str
    event_types: frozenset[str] = MACRO_TYPES
    point_in_time: bool = True
    missing: bool = False
    version: str = "synthetic_v1"

    def __post_init__(self):
        for stamp in (self.available_at, self.covered_from, self.covered_until):
            aware(stamp)
        if (
            self.covered_from >= self.covered_until
            or not self.source.strip()
            or not self.version.strip()
            or not self.event_types <= MACRO_TYPES
            or type(self.point_in_time) is not bool
            or type(self.missing) is not bool
        ):
            raise ValueError("Invalid macro calendar coverage")


class MacroEventProvider(Protocol):
    def snapshot(
        self, asof: datetime
    ) -> tuple[MacroEventCoverage | None, tuple[MacroEvent, ...]]: ...


class InMemoryMacroEventProvider:
    """Synthetic PIT fixture. Real historical schedule provenance is Issue #5D/E."""

    def __init__(self, events=(), coverage=()):
        self.events, self.coverage = tuple(events), tuple(coverage)

    def snapshot(self, asof):
        aware(asof)
        known = [c for c in self.coverage if c.available_at <= asof]
        coverage = max(known, key=lambda c: c.available_at) if known else None
        return coverage, tuple(e for e in self.events if e.scheduled_known_at <= asof)


@dataclass(frozen=True)
class MacroWindow:
    start: datetime
    end: datetime
    event: MacroEvent

    def record(self):
        e = self.event
        return {
            "event_id": e.event_id,
            "type": e.event_type,
            "known_at": e.scheduled_known_at.isoformat(),
            "scheduled_at": e.scheduled_at.isoformat(),
            "source": e.source,
            "revision": e.revision,
            "window": [self.start.isoformat(), self.end.isoformat()],
        }


class MacroEventGate:
    def __init__(
        self,
        config: "ConstraintConfig",
        calendar: SessionCalendar,
        provider: MacroEventProvider | None = None,
    ):
        self.config, self.calendar = config, calendar
        self.provider = provider or InMemoryMacroEventProvider()

    def known(self, asof):
        try:
            coverage, supplied = self.provider.snapshot(asof)
        except (OSError, ValueError):
            return True, ()  # Calendar outage is unknown; never fabricate no-event coverage.
        bounds = self.calendar.bounds(asof)
        start = bounds[0] if bounds else asof
        if self.config.macro_fomc_next_session and bounds:
            previous = self.calendar.shift(asof.astimezone(NY).date(), -1)
            start = self.calendar.session(previous).market_open
        end = bounds[1] if bounds else asof
        missing = (
            coverage is None
            or coverage.available_at > asof
            or coverage.missing
            or not coverage.point_in_time
            or coverage.covered_from > start
            or coverage.covered_until < end
            or not set(self.config.macro_event_types) <= coverage.event_types
        )
        latest = {}
        for event in sorted(supplied, key=lambda e: (e.scheduled_known_at, e.revision)):
            if event.scheduled_known_at > asof:
                continue
            previous = latest.get(event.event_id)
            if previous and event.revision <= previous.revision and event != previous:
                missing = True
            latest[event.event_id] = event
        visible = tuple(
            e
            for e in latest.values()
            if not e.cancelled and e.event_type in self.config.macro_event_types
        )
        missing |= any(e.missing for e in visible)
        return missing, visible

    def windows(self, events, *, pressure=False):
        settings = self.config
        windows = []
        for event in events:
            stamp = event.scheduled_at.astimezone(NY)
            session = self.calendar.session(stamp.date())
            if session is None:
                continue  # Never manufacture an event on the next trading day.
            opening, closing = session.market_open, session.market_close
            before = timedelta(minutes=settings.macro_pre_release_minutes)
            after = timedelta(minutes=settings.macro_post_release_minutes)
            if pressure:
                start, end = stamp - before, stamp + after
                if stamp < opening:
                    start, end = opening, opening + after
            elif event.event_type.startswith("FOMC_"):
                lead = settings.macro_fomc_statement_lead_minutes
                if event.event_type == "FOMC_PRESS":
                    lead = settings.macro_fomc_press_lead_minutes
                start, end = stamp - timedelta(minutes=lead), closing
            elif stamp < opening and event.event_type in {"CPI", "NFP", "PCE", "PPI"}:
                wait = (
                    settings.macro_ppi_wait_minutes
                    if event.event_type == "PPI"
                    else settings.macro_major_wait_minutes
                )
                start, end = opening, opening + timedelta(minutes=wait)
            else:
                start, end = stamp - before, stamp + after
            # Late-known surprise schedules cannot create a retroactive blackout.
            visible_from = event.scheduled_known_at
            if event.scheduled_known_at > event.scheduled_at:
                if event.published_at is None:
                    continue
                visible_from = max(visible_from, event.published_at)
            start = max(start, opening, visible_from)
            end = min(end, closing)
            if start < end:
                windows.append(MacroWindow(start, end, event))
            if (
                not pressure
                and event.event_type.startswith("FOMC_")
                and settings.macro_fomc_next_session
            ):
                following = self.calendar.session(self.calendar.shift(stamp.date(), 1))
                start = max(following.market_open, visible_from)
                end = min(
                    following.market_close,
                    following.market_open
                    + timedelta(minutes=settings.macro_fomc_next_wait_minutes),
                )
                if start < end:
                    windows.append(MacroWindow(start, end, event))
        return tuple(windows)

    def active(self, asof, *, pressure=False):
        if not self.config.macro_events_enabled:
            return False, ()
        missing, events = self.known(asof)
        return missing, tuple(
            w for w in self.windows(events, pressure=pressure) if w.start <= asof < w.end
        )

    def check(self, intent: Intent, state: State, context: MarketContext) -> Decision:
        if intent.quantity <= 0 or not self.config.macro_events_enabled:
            return ALLOW
        if context.data_frequency not in {"1m", "5m", "15m", "30m", "irregular"}:
            raise ValueError("Macro windows require intraday timestamps")
        missing, events = self.known(context.asof)
        if missing and self.config.missing_macro_calendar == "reject_new_entries":
            return Decision(Action.REJECT, "macro_calendar_missing")
        windows = self.windows(events)
        active = [w for w in windows if w.start <= context.asof < w.end]
        if not active:
            return Decision(Action.ALLOW, "macro_calendar_opt_out") if missing else ALLOW
        resume = max(w.end for w in active)
        # Union may be extended by a known window that starts before resumption.
        for window in sorted(windows, key=lambda w: w.start):
            if context.asof < window.start <= resume:
                resume = max(resume, window.end)
        return Decision(
            Action.DEFER,
            "macro_blackout",
            "Known macro schedule; recheck at legal observed bar",
            data={
                "events": [w.record() for w in active],
                "resume_at": resume.isoformat(),
                "calendar_missing": missing,
            },
        )
