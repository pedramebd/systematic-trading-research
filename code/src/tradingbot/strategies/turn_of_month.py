"""Turn-of-the-month: SPY over Day -1..+3, cash otherwise.

Pre-registered as trial 4 (docs/trials/turn_of_month.md). Section 6:

The turn of the month (McConnell and Xu 2008; Lakonishok and Smidt 1988) is
Day -1 (the last trading day of a month) through Day +3 (the third trading
day of the next), measured close to close. Earning exactly those four daily
returns means owning SPY at the closes of Days -2, -1, +1 and +2 and selling
at the close of Day +3. With market-on-close fills (`fill="close"`, the
primary mode) the target decided for session t fills at t's close, so:

    target 1.0  on sessions -2, -1, +1, +2;  0.0 otherwise.

The secondary open-fill mode (`fill="open"`) holds from Day -1's open to
Day +4's open:

    target 1.0  on sessions -1, +1, +2, +3;  0.0 otherwise.

Where t sits in its month:

- from the START, `p(t)`: 1 + the number of visible bars in t's month. It
  needs only bars strictly before t, so it is point-in-time by construction.
- from the END, `k(t)`: sessions from t to the month's end, inclusive, in the
  SCHEDULED NYSE calendar (`us_calendar.scheduled_sessions_to_month_end`),
  which was known in advance. One-off closures were not, so they are not in
  it (Decision D8): in October 2012 (Hurricane Sandy) the strategy sees the
  31st as Day -1 and the 26th as Day -4, and buys at the 31st's close.

The decision reads no price. No fitting, no state, no I/O.
"""

from __future__ import annotations

from tradingbot.data.pit import PointInTimeView
from tradingbot.data.us_calendar import scheduled_sessions_to_month_end
from tradingbot.strategies.base import Strategy

#: (last sessions of the month, first sessions of the month) held at the
#: target, per fill mode. See the module docstring.
WINDOWS: dict[str, tuple[int, int]] = {"close": (2, 2), "open": (1, 3)}

#: Bars of history needed to place t within its month: a month has at most
#: 23 sessions, so the last 25 visible bars always reach the previous month.
_LOOKBACK = 25


class TurnOfMonth(Strategy):
    name = "turn_of_month"

    def __init__(self, symbol: str = "SPY", fill: str = "close"):
        if fill not in WINDOWS:
            raise ValueError(f"fill must be one of {sorted(WINDOWS)}, not {fill!r}")
        self.symbol = symbol
        self.fill = fill
        self.last_sessions, self.first_sessions = WINDOWS[fill]

    def position_from_start(self, view: PointInTimeView) -> int | None:
        """p(t): 1-based position of `view.as_of` in its month, or None when
        no earlier bar is visible (t cannot be placed)."""
        visible = view.history(f"{self.symbol}_close", lookback=_LOOKBACK)
        if visible.empty:
            return None
        as_of = view.as_of
        in_month = (visible.index.year == as_of.year) & (visible.index.month == as_of.month)
        return 1 + int(in_month.sum())

    def in_window(self, view: PointInTimeView) -> bool:
        p = self.position_from_start(view)
        if p is None:
            return False
        k = scheduled_sessions_to_month_end(view.as_of)
        return 1 <= k <= self.last_sessions or p <= self.first_sessions

    def target_weights(self, view: PointInTimeView) -> dict[str, float]:
        return {self.symbol: 1.0 if self.in_window(view) else 0.0}
