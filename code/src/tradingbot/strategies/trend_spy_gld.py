"""Faber (2007) 10-month trend filter on SPY and GLD, 50% sleeve each.

Pre-registered as trial 3 (docs/trials/trend_spy_gld.md). The rule, section 6:

    At each month-end close, for each asset: if its adjusted close is
    ABOVE the mean of its last 10 month-end adjusted closes (this one
    included), its 50% sleeve is invested for the next month; otherwise
    (ties included) that sleeve is cash.

A month-end is the last session of a calendar month in the joined SPY/GLD
calendar. The harness executes at the open of session t using bars strictly
before t, so at `view.as_of` = t every calendar month BEFORE t's month is
complete, and its last visible bar is its month-end. t's own month is
excluded: its month-end has not happened. On the first session of a month
the governing month-end is therefore the bar immediately before it, and the
switch executes that same morning.

Fewer than 10 complete month-ends visible -> that sleeve is cash. No fitting,
no state, no I/O.
"""

from __future__ import annotations

import numpy as np

from tradingbot.data.pit import PointInTimeView
from tradingbot.strategies.base import Strategy


class TrendSpyGld(Strategy):
    name = "trend_spy_gld"

    def __init__(
        self,
        symbols: tuple[str, ...] = ("SPY", "GLD"),
        lookback_months: int = 10,
        sleeve_weight: float = 0.5,
    ):
        self.symbols = tuple(symbols)
        self.lookback_months = lookback_months
        self.sleeve_weight = sleeve_weight

    def month_end_closes(self, view: PointInTimeView, symbol: str) -> np.ndarray:
        """Closes of every complete month visible at `view.as_of`, oldest first."""
        closes = view.history(f"{symbol}_close")
        if closes.empty:
            return np.empty(0)
        index = closes.index
        month = index.year * 12 + index.month
        current = view.as_of.year * 12 + view.as_of.month
        # A bar is a month-end when the next bar -- or, for the last visible
        # bar, the decision session itself -- falls in a different month.
        following = np.append(month[1:], current)
        is_month_end = month != following
        return closes.to_numpy(dtype=float)[is_month_end]

    def sleeve_invested(self, view: PointInTimeView, symbol: str) -> bool:
        month_ends = self.month_end_closes(view, symbol)
        if len(month_ends) < self.lookback_months:
            return False
        window = month_ends[-self.lookback_months :]
        return bool(month_ends[-1] > window.mean())

    def target_weights(self, view: PointInTimeView) -> dict[str, float]:
        return {
            symbol: self.sleeve_weight if self.sleeve_invested(view, symbol) else 0.0
            for symbol in self.symbols
        }
