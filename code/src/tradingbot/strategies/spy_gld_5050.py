"""Control for trend_spy_gld: 50% SPY / 50% GLD, always held.

Defined in section 8 of the trend_spy_gld pre-registration. Identical data,
execution, costs, rebalance schedule and sample as the strategy, without the
trend filter. It is a control, not a candidate, so it is excluded from the
multiple-testing count (registry entry `spy_gld_5050`).
"""

from __future__ import annotations

from tradingbot.data.pit import PointInTimeView
from tradingbot.strategies.base import Strategy


class SpyGld5050(Strategy):
    name = "spy_gld_5050"

    def __init__(self, symbols: tuple[str, ...] = ("SPY", "GLD"), weight: float = 0.5):
        self.symbols = tuple(symbols)
        self.weight = weight

    def target_weights(self, view: PointInTimeView) -> dict[str, float]:
        if len(view) == 0:
            return {}
        return {symbol: self.weight for symbol in self.symbols}
