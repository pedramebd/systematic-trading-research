"""US equity session calendar: independent cross-check for SPY's trading dates.

The AUTHORITY for which days traded is SPY's actual daily bars. This module
exists only to say whether a weekday missing from SPY is explained by a known
NYSE closure, or is a data gap that must be reported rather than papered over.

pandas_market_calendars is not a dependency, so the NYSE rules are built from
pandas' holiday primitives. Caveats, stated rather than hidden:

- pandas' USFederalHolidayCalendar is NOT the NYSE calendar: it includes
  Columbus Day and Veterans Day (NYSE open) and omits Good Friday (NYSE shut).
  The rules below are NYSE's, not the federal list.
- NYSE does not close on a Friday when New Year's Day falls on Saturday (e.g.
  2021-12-31 traded). `nearest_workday` would wrongly shift it; the rule below
  only moves Sunday holidays to Monday for New Year. Independence Day,
  Juneteenth and Christmas DO move Saturday -> Friday.
- Juneteenth is an NYSE holiday from 2022 only.
- One-off closures (national days of mourning, weather) cannot be derived
  from rules and are listed explicitly in SPECIAL_CLOSURES. The list is
  complete to the author's knowledge from 2004 (the pre-2017 entries were
  added for the turn_of_month trial's data audit); a closure missing from it
  would surface as an "unexplained" gap, which is the intended failure mode.
- `scheduled_sessions_to_month_end` uses the RULES ONLY, never
  SPECIAL_CLOSURES: it answers "what did the published schedule say", which a
  strategy may know in advance. A one-off closure was not known in advance.
"""

from __future__ import annotations

from datetime import datetime
from functools import cache

import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar,
    GoodFriday,
    Holiday,
    USLaborDay,
    USMartinLutherKingJr,
    USMemorialDay,
    USPresidentsDay,
    USThanksgivingDay,
    nearest_workday,
    sunday_to_monday,
)

EQUITY_TZ = "America/New_York"
EQUITY_OPEN = pd.Timedelta(hours=9, minutes=30)

SPECIAL_CLOSURES: dict[str, str] = {
    "2004-06-11": "National Day of Mourning, President Ronald Reagan",
    "2007-01-02": "National Day of Mourning, President Gerald Ford",
    "2012-10-29": "Hurricane Sandy",
    "2012-10-30": "Hurricane Sandy",
    "2018-12-05": "National Day of Mourning, President George H. W. Bush",
    "2025-01-09": "National Day of Mourning, President Jimmy Carter",
}


class NYSEHolidayCalendar(AbstractHolidayCalendar):
    rules = [
        Holiday("NewYearsDay", month=1, day=1, observance=sunday_to_monday),
        USMartinLutherKingJr,
        USPresidentsDay,
        GoodFriday,
        USMemorialDay,
        Holiday(
            "Juneteenth",
            month=6,
            day=19,
            start_date=datetime(2022, 1, 1),
            observance=nearest_workday,
        ),
        Holiday("IndependenceDay", month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday("Christmas", month=12, day=25, observance=nearest_workday),
    ]


def expected_closures(start: pd.Timestamp | str, end: pd.Timestamp | str) -> pd.DatetimeIndex:
    """Weekday NYSE full-day closures between start and end, inclusive."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    rule_based = NYSEHolidayCalendar().holidays(start=start, end=end)
    special = pd.DatetimeIndex(pd.to_datetime(list(SPECIAL_CLOSURES)))
    special = special[(special >= start) & (special <= end)]
    closures = rule_based.union(special)
    return closures[closures.dayofweek < 5]


def cross_check_trading_dates(trading_dates: pd.DatetimeIndex) -> dict[str, list[str]]:
    """Compare weekdays missing from `trading_dates` with expected closures.

    Returns
    -------
    unexplained_missing:
        Weekdays absent from the data that no rule explains -- data gaps.
    closures_that_traded:
        Expected closures on which the data nevertheless has a bar -- either
        the rule is wrong or the data is.
    """
    dates = pd.DatetimeIndex(trading_dates).normalize().unique().sort_values()
    weekdays = pd.bdate_range(dates.min(), dates.max())
    missing = weekdays.difference(dates)
    closures = expected_closures(dates.min(), dates.max())
    return {
        "unexplained_missing": [str(d.date()) for d in missing.difference(closures)],
        "closures_that_traded": [str(d.date()) for d in closures.intersection(dates)],
    }


def equity_open_utc(day: pd.Timestamp) -> pd.Timestamp:
    """09:30 New York on `day` (tz-naive date), as a UTC timestamp, DST-aware.

    This is the instant a weekly decision is executed. The backtest harness
    uses it as the point-in-time boundary for event-stamped sources (see
    PointInTimeView.source), and the weekend-variance builder asserts every
    weekend window ends strictly before it.
    """
    local = (pd.Timestamp(day).normalize() + EQUITY_OPEN).tz_localize(EQUITY_TZ)
    return local.tz_convert("UTC")


def is_trading_day(day: pd.Timestamp | str | datetime) -> bool:
    """True if `day` is a full NYSE session.

    Added for the execution time gate, which must not place an order on a day
    the exchange is shut. It composes the rules already in this module rather
    than restating them, so a holiday fixed here is fixed in both places.

    Early closes (the day after Thanksgiving, Christmas Eve) are trading days
    and return True: the session opens at 09:30 as usual, and execution
    happens shortly after the open, well before the 13:00 close.
    """
    stamp = pd.Timestamp(day).normalize()
    if stamp.dayofweek >= 5:
        return False
    return stamp not in expected_closures(stamp, stamp)


@cache
def _scheduled_sessions(year: int, month: int) -> tuple[pd.Timestamp, ...]:
    first = pd.Timestamp(year=year, month=month, day=1)
    last = first + pd.offsets.MonthEnd(0)
    holidays = NYSEHolidayCalendar().holidays(start=first, end=last)
    return tuple(pd.bdate_range(first, last).difference(holidays))


def scheduled_sessions_to_month_end(day: pd.Timestamp | str | datetime) -> int:
    """Sessions from `day` to the end of its month, inclusive, in the
    SCHEDULED NYSE calendar: weekdays less the rule-based holidays, WITHOUT
    one-off closures (SPECIAL_CLOSURES), which were not known in advance.

    1 on the last scheduled session of a month, 2 on the one before, and so
    on. `day` itself counts only if it is a scheduled session. Used by the
    turn_of_month strategy to locate Days -2 and -1 before they happen
    (turn_of_month pre-registration, section 6 and Decision D8; summary in
    docs/trials/turn_of_month.md).
    """
    stamp = pd.Timestamp(day).normalize()
    return sum(1 for session in _scheduled_sessions(stamp.year, stamp.month) if session >= stamp)
