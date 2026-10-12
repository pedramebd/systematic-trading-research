"""The control.

Every strategy is measured against this. If a clever idea cannot beat holding
SPY on a risk-adjusted basis after costs, the honest conclusion is to hold SPY.
"""

from __future__ import annotations

from tradingbot.data.pit import PointInTimeView
from tradingbot.strategies.base import Strategy


class BuyAndHold(Strategy):
    name = "buy_and_hold"

    def __init__(self, symbol: str = "SPY", weight: float = 1.0):
        self.symbol = symbol
        self.weight = weight

    def target_weights(self, view: PointInTimeView) -> dict[str, float]:
        if len(view) == 0:
            return {}
        return {self.symbol: self.weight}


class ZeroSignal(Strategy):
    """Never trades. Used by the zero-signal invariant test: a strategy that
    does nothing must produce exactly zero P&L. If it does not, the
    accounting is wrong."""

    name = "zero_signal"

    def target_weights(self, view: PointInTimeView) -> dict[str, float]:
        return {}
