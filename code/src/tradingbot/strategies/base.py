"""Strategy interface.

Constitution section II: strategies are pure functions of point-in-time data.
They do no I/O, they do not know the account equity, and they cannot size
their own positions -- they express intent as target weights, and the
portfolio and risk layers decide what that means in currency.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from tradingbot.data.pit import PointInTimeView


class Strategy(ABC):
    name: str = "unnamed"
    #: Realised annual volatility the strategy was validated against. The risk
    #: layer halts new positions when live vol exceeds a multiple of this.
    assumed_annual_vol: float = 0.20

    @abstractmethod
    def target_weights(self, view: PointInTimeView) -> dict[str, float]:
        """Desired portfolio weights given only what was knowable at
        `view.as_of`. Values are fractions of equity; they must not sum in
        absolute value to more than 1.0 without leverage permission."""

    def warmup_periods(self) -> int:
        """Bars required before the strategy will emit a signal."""
        return 0
