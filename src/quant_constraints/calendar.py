"""Exchange sessions and distinct NSCC settlement business dates."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ml4t.backtest.accounting.settlement import _settlement_days
from ml4t.backtest.calendar import get_calendar_sessions

from .models import aware

NY = ZoneInfo("America/New_York")
# Rules are effective on trade date. A fixed-cycle override is also supported.
SETTLEMENT_RULES = ((date(1995, 6, 7), 3), (date(2017, 9, 5), 2), (date(2024, 5, 28), 1))


class SessionCalendar:
    def session(self, day: date):
        return get_calendar_sessions("NYSE", day.year).get(day)

    def shift(self, day: date, count: int) -> date:
        step = 1 if count >= 0 else -1
        remaining = abs(count)
        while remaining:
            day += timedelta(days=step)
            if self.session(day) is not None:
                remaining -= 1
        return day

    def on_or_after(self, day: date) -> date:
        while self.session(day) is None:
            day += timedelta(days=1)
        return day

    def distance(self, start: date, end: date) -> int:
        if end < start:
            return -self.distance(end, start)
        count = 0
        while start < end:
            start += timedelta(days=1)
            count += self.session(start) is not None
        return count

    def bounds(self, asof: datetime) -> tuple[datetime, datetime] | None:
        item = self.session(aware(asof).astimezone(NY).date())
        return (item.market_open, item.market_close) if item else None

    def first_affected_session(self, announcement: datetime) -> date:
        """Release at/after the actual close first affects the next session."""
        stamp = aware(announcement).astimezone(NY)
        day = stamp.date()
        session = self.session(day)
        if session is None:
            return self.on_or_after(day)
        return self.shift(day, 1) if stamp >= session.market_close else day


def settlement_date(
    trade: date, cycle: str = "historical", holidays: frozenset[date] = frozenset()
) -> date:
    if cycle not in {"historical", "T+1", "T+2"}:
        raise ValueError("Invalid settlement cycle")
    if trade < SETTLEMENT_RULES[0][0]:
        raise ValueError("Settlement dates before 1995-06-07 are unsupported")
    delay = (
        int(cycle[-1])
        if cycle != "historical"
        else max((rule for rule in SETTLEMENT_RULES if rule[0] <= trade), key=lambda rule: rule[0])[
            1
        ]
    )
    while delay:
        trade += timedelta(days=1)
        if trade in _settlement_days(trade.year) and trade not in holidays:
            delay -= 1
    return trade
