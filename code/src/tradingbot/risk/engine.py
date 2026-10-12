"""Risk enforcement.

Constitution section I.2: a limit that exists only in prose is not a limit.
Everything in CONSTITUTION.md section III is enforced here, deterministically,
with no model in the loop.

The engine is pure: it takes state and a proposed order, and returns a
decision. It performs no I/O and holds no hidden state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from tradingbot.config import Limits, load_limits, tradeable_symbols
from tradingbot.risk.state import AccountState, Halt, HaltAction, HaltRegistry, HaltScope


class Severity(str, Enum):
    WARN = "warn"
    BLOCK = "block"
    HALT = "halt"


@dataclass(frozen=True)
class Violation:
    rule: str
    severity: Severity
    message: str
    observed: float | None = None
    limit: float | None = None


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    quantity: float
    price: float
    strategy: str
    risk_amount: float = 0.0  # currency at risk if the stop is hit

    @property
    def notional(self) -> float:
        return abs(self.quantity * self.price)


@dataclass
class Decision:
    approved: bool
    violations: list[Violation] = field(default_factory=list)

    @property
    def blocking(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is not Severity.WARN]


class RiskEngine:
    def __init__(self, limits: Limits | None = None, halts: HaltRegistry | None = None):
        self.limits = limits or load_limits()
        self.halts = halts or HaltRegistry()

    # ---------- portfolio-level checks, run every cycle ----------

    def check_account(self, state: AccountState) -> list[Violation]:
        """Loss limits. Returns violations and registers halts as required."""
        loss = self.limits.loss
        found: list[Violation] = []

        checks = [
            (
                state.drawdown_pct(),
                loss.drawdown_stop_pct,
                "drawdown_stop",
                HaltScope.FULL,
                "full stop: drawdown from peak",
            ),
            (
                state.week_loss_pct(),
                loss.weekly_loss_pct,
                "weekly_loss",
                HaltScope.WEEK,
                "halt for the week: weekly loss",
            ),
            (
                state.day_loss_pct(),
                loss.daily_loss_pct,
                "daily_loss",
                HaltScope.DAY,
                "halt for the day: daily loss",
            ),
        ]

        active = self.halts.active_scopes()
        for observed, limit, rule, scope, text in checks:
            if observed >= limit:
                # Already halted at this scope -- do not re-register. Without
                # this the engine emits one event per bar for the rest of the
                # backtest, and one alert per cycle in live trading.
                if scope in active:
                    continue
                found.append(
                    Violation(
                        rule,
                        Severity.HALT,
                        f"{text} {observed:.2f}% >= {limit:.2f}%",
                        observed,
                        limit,
                    )
                )
                self.halts.add(
                    Halt(
                        scope=scope,
                        reason=f"{rule}: {observed:.2f}% >= {limit:.2f}%",
                        triggered_at=state.as_of or datetime.utcnow(),
                        action=HaltAction.FLATTEN,
                    )
                )

        for strategy, streak in state.consecutive_losses.items():
            if streak >= loss.max_consecutive_losses:
                if self.halts.is_halted(strategy):
                    continue
                found.append(
                    Violation(
                        "consecutive_losses",
                        Severity.HALT,
                        f"{strategy}: {streak} consecutive losses",
                        streak,
                        loss.max_consecutive_losses,
                    )
                )
                self.halts.add(
                    Halt(
                        scope=HaltScope.STRATEGY,
                        strategy=strategy,
                        reason=f"{streak} consecutive losses",
                        triggered_at=state.as_of or datetime.utcnow(),
                        action=HaltAction.FLATTEN,
                    )
                )

        return found

    def exposure_multiplier(self, state: AccountState) -> float:
        """How much of its target exposure the book may hold right now.

        Constitution III: breaching `drawdown_warn_pct` REDUCES exposure, it
        does not halt. A halt at the warn level meant one bad month flattened
        the book for the rest of the backtest, which made the benchmark
        meaningless and told us nothing about the strategy.

        This is a pure function of current state with no stored halt, so it
        lifts by itself once the drawdown recovers. That is safe because it
        can only ever reduce risk -- `drawdown_stop_pct` is still a permanent
        halt requiring a human, per constitution V.
        """
        if state.drawdown_pct() >= self.limits.loss.drawdown_warn_pct:
            return self.limits.loss.drawdown_warn_exposure
        return 1.0

    # ---------- order-level checks ----------

    def check_order(self, order: OrderRequest, state: AccountState) -> Decision:
        violations: list[Violation] = []
        pos = self.limits.position
        perms = self.limits.permissions

        existing = state.positions.get(order.symbol)
        held_qty = existing.quantity if existing else 0.0
        held_value = existing.market_value if existing else 0.0
        post_qty = held_qty + order.quantity
        # A reduction moves an existing position toward zero without crossing
        # it. Reducing risk is always allowed (constitution I.3), so a
        # reduction skips the halt, universe and size limits -- all of which
        # it can only move toward compliance. Only a FREEZE, bad equity or
        # stale data can stop it.
        reducing = held_qty != 0.0 and 0.0 <= post_qty / held_qty < 1.0

        if self.halts.freezes(order.strategy):
            violations.append(
                Violation(
                    "frozen",
                    Severity.BLOCK,
                    "a FREEZE halt is active: no orders of any kind, sells included",
                )
            )

        if reducing:
            if state.equity <= 0:
                violations.append(Violation("equity", Severity.BLOCK, "non-positive equity"))
            stale = self._staleness_violation(state)
            if stale:
                violations.append(stale)
            if not perms.allow_live_trading:
                violations.append(
                    Violation(
                        "paper_only",
                        Severity.WARN,
                        "live trading disabled; this order is paper only",
                    )
                )
            blocking = [v for v in violations if v.severity is not Severity.WARN]
            return Decision(approved=not blocking, violations=violations)

        if self.halts.is_halted(order.strategy):
            violations.append(
                Violation(
                    "halted",
                    Severity.BLOCK,
                    f"trading halted; active: "
                    f"{sorted(s.value for s in self.halts.active_scopes())}",
                )
            )

        if order.symbol not in tradeable_symbols():
            violations.append(
                Violation(
                    "universe",
                    Severity.BLOCK,
                    f"{order.symbol} is not in config/universe.yaml execution list",
                )
            )

        # Shorting means ENDING below zero -- not merely selling. A sale that
        # reduces a long is handled above as a reduction.
        if post_qty < -1e-9 and not perms.allow_shorting:
            violations.append(
                Violation(
                    "shorting",
                    Severity.BLOCK,
                    f"order would leave {order.symbol} at {post_qty:.4f} shares; "
                    "shorting is disabled in config/limits.yaml",
                )
            )

        if state.equity <= 0:
            violations.append(Violation("equity", Severity.BLOCK, "non-positive equity"))
            return Decision(approved=False, violations=violations)

        risk_pct = 100.0 * order.risk_amount / state.equity
        if risk_pct > pos.max_risk_per_trade_pct:
            violations.append(
                Violation(
                    "max_risk_per_trade",
                    Severity.BLOCK,
                    f"trade risk {risk_pct:.2f}% > {pos.max_risk_per_trade_pct:.2f}%",
                    risk_pct,
                    pos.max_risk_per_trade_pct,
                )
            )

        post_value = abs(held_value + order.quantity * order.price)
        post_instrument_pct = 100.0 * post_value / state.equity
        if post_instrument_pct > pos.max_instrument_pct:
            violations.append(
                Violation(
                    "max_instrument",
                    Severity.BLOCK,
                    f"{order.symbol} would be {post_instrument_pct:.2f}% "
                    f"> {pos.max_instrument_pct:.2f}%",
                    post_instrument_pct,
                    pos.max_instrument_pct,
                )
            )

        # Replace this symbol's exposure with its post-trade exposure. Adding
        # the order's notional instead counted every SELL as extra exposure.
        post_gross = state.gross_exposure - abs(held_value) + post_value
        post_gross_pct = 100.0 * post_gross / state.equity
        if post_gross_pct > pos.max_gross_exposure_pct:
            violations.append(
                Violation(
                    "max_gross_exposure",
                    Severity.BLOCK,
                    f"gross would be {post_gross_pct:.2f}% > {pos.max_gross_exposure_pct:.2f}%",
                    post_gross_pct,
                    pos.max_gross_exposure_pct,
                )
            )

        if not perms.allow_live_trading:
            violations.append(
                Violation(
                    "paper_only", Severity.WARN, "live trading disabled; this order is paper only"
                )
            )

        stale = self._staleness_violation(state)
        if stale:
            violations.append(stale)

        blocking = [v for v in violations if v.severity is not Severity.WARN]
        return Decision(approved=not blocking, violations=violations)

    def _staleness_violation(self, state: AccountState) -> Violation | None:
        if state.data_timestamp is None or state.as_of is None:
            return None
        age_min = (state.as_of - state.data_timestamp).total_seconds() / 60.0
        limit = self.limits.operational.max_data_staleness_minutes
        if age_min > limit:
            return Violation(
                "data_staleness",
                Severity.BLOCK,
                f"data is {age_min:.1f} min old > {limit} min",
                age_min,
                float(limit),
            )
        return None
