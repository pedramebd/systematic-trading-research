"""Account and halt state.

Constitution section V: a halt never lifts itself. `clear()` exists so a
human can reset it deliberately, and it demands a written reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class HaltScope(str, Enum):
    NONE = "none"
    STRATEGY = "strategy"
    DAY = "day"
    WEEK = "week"
    FULL = "full"


class HaltAction(str, Enum):
    """What a halt does to positions already held.

    FLATTEN -- loss limits. Close every position and buy nothing. Reducing
               risk is always allowed (constitution I.3), and this is what the
               backtest has always done on a halt, so live and backtest agree.
    FREEZE  -- something is broken: the book does not match the broker, the
               state file is unreadable, an order went missing, or a human
               pressed stop. Send NOTHING, not even a sell. If you do not know
               what you hold, you must not trade at all.

    FREEZE is the default, because it is the safe answer when in doubt.
    """

    FLATTEN = "flatten"
    FREEZE = "freeze"


@dataclass(frozen=True)
class Halt:
    scope: HaltScope
    reason: str
    triggered_at: datetime
    strategy: str | None = None
    action: HaltAction = HaltAction.FREEZE

    def __post_init__(self) -> None:
        if self.scope is HaltScope.STRATEGY and not self.strategy:
            raise ValueError("a STRATEGY halt must name a strategy")
        if not self.reason.strip():
            raise ValueError("a halt requires a reason")


@dataclass
class HaltRegistry:
    """Active halts. Additive by design; only a human removes one."""

    halts: list[Halt] = field(default_factory=list)
    resume_log: list[tuple[datetime, str, str]] = field(default_factory=list)

    def add(self, halt: Halt) -> None:
        self.halts.append(halt)

    @staticmethod
    def _applies(halt: Halt, strategy: str | None) -> bool:
        if halt.scope in (HaltScope.FULL, HaltScope.DAY, HaltScope.WEEK):
            return True
        return halt.scope is HaltScope.STRATEGY and halt.strategy == strategy

    def is_halted(self, strategy: str | None = None) -> bool:
        return any(self._applies(h, strategy) for h in self.halts)

    def freezes(self, strategy: str | None = None) -> bool:
        """True if any applicable halt forbids ALL orders, sells included."""
        return any(self._applies(h, strategy) and h.action is HaltAction.FREEZE for h in self.halts)

    def active_scopes(self) -> set[HaltScope]:
        return {halt.scope for halt in self.halts}

    def expire(self, scope: HaltScope) -> int:
        """Remove halts whose scope is time-bounded by definition.

        A DAY halt means "halt for the remainder of the day"; when the day
        ends, so does the halt. Same for WEEK. This is NOT the human reset
        path -- FULL halts are deliberately not expirable here, because
        "drawdown stop" means a human decides, not a calendar.
        """
        if scope not in (HaltScope.DAY, HaltScope.WEEK):
            raise ValueError(f"{scope.value} halts do not expire; a human must clear() them")
        before = len(self.halts)
        self.halts = [h for h in self.halts if h.scope is not scope]
        return before - len(self.halts)

    def clear(self, scope: HaltScope, reason: str, when: datetime, by: str) -> None:
        """Human reset. Constitution V: never automatic, always justified."""
        if not reason.strip():
            raise ValueError("resuming from a halt requires a written reason")
        if not by.strip():
            raise ValueError("resuming from a halt requires an identified human")
        self.halts = [h for h in self.halts if h.scope is not scope]
        self.resume_log.append((when, f"{scope.value}:{by}", reason))


@dataclass
class Position:
    symbol: str
    quantity: float
    market_value: float


@dataclass
class AccountState:
    """Everything the risk engine needs to decide. No I/O, no broker calls."""

    equity: float
    peak_equity: float
    day_start_equity: float
    week_start_equity: float
    positions: dict[str, Position] = field(default_factory=dict)
    consecutive_losses: dict[str, int] = field(default_factory=dict)
    as_of: datetime | None = None
    data_timestamp: datetime | None = None
    session_date: date | None = None

    @property
    def gross_exposure(self) -> float:
        return sum(abs(p.market_value) for p in self.positions.values())

    @property
    def gross_exposure_pct(self) -> float:
        if self.equity <= 0:
            return float("inf")
        return 100.0 * self.gross_exposure / self.equity

    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return 100.0 * (self.peak_equity - self.equity) / self.peak_equity

    def day_loss_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return 100.0 * (self.day_start_equity - self.equity) / self.day_start_equity

    def week_loss_pct(self) -> float:
        if self.week_start_equity <= 0:
            return 0.0
        return 100.0 * (self.week_start_equity - self.equity) / self.week_start_equity
