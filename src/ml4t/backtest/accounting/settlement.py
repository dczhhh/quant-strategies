"""US ordinary-equity settlement dates, independent of trading bars/sessions.

SIFMA's US calendar supplies regular banking/market holidays. Good Friday is
always removed for NSCC ordinary-equity settlement, even when bonds trade.
Extra NSCC/DTC closures must be supplied from the relevant official notices.
"""

from datetime import date, timedelta
from functools import lru_cache

from pandas.tseries.holiday import AbstractHolidayCalendar, GoodFriday

from ..calendar import get_calendar


class _GoodFridayCalendar(AbstractHolidayCalendar):
    rules = [GoodFriday]


@lru_cache(maxsize=128)
def _settlement_days(year: int) -> frozenset[date]:
    start, end = date(year, 1, 1), date(year, 12, 31)
    days = {stamp.date() for stamp in get_calendar("SIFMA_US").valid_days(start, end)}
    good_fridays = {stamp.date() for stamp in _GoodFridayCalendar().holidays(start, end)}
    return frozenset(days - good_fridays)


def us_equity_settlement_date(
    trade_date: date, extra_holidays: frozenset[date] = frozenset()
) -> date:
    """Return T+N using the regime effective on the trade date (1995 onward)."""
    if trade_date < date(1995, 6, 7):
        raise ValueError("US cash settlement supports trade dates from 1995-06-07 onward")
    delay = 1 if trade_date >= date(2024, 5, 28) else 2
    if trade_date < date(2017, 9, 5):
        delay = 3
    result = trade_date
    while delay:
        result += timedelta(days=1)
        if result in _settlement_days(result.year) and result not in extra_holidays:
            delay -= 1
    return result
