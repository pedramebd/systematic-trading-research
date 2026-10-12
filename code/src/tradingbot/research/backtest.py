"""Event-driven backtest engine.

Timing convention, stated once and enforced everywhere:

    decide using data strictly BEFORE t  ->  execute at t's OPEN  ->  mark at t's CLOSE

The strategy never sees the DataFrame. It receives a PointInTimeView whose
`as_of` is t, so a bar stamped t is invisible to it. This makes look-ahead a
structural impossibility rather than something to be careful about.

RISK ENFORCEMENT
----------------
By default the backtest honours CONSTITUTION.md: halts fire, limits bind, and
the equity curve reflects what the live system would actually have done. This
matters because an unconstrained backtest flatters a strategy the live system
would have halted, and you would then spend weeks explaining the divergence.

Two modelling assumptions, stated because they both affect the result:

1. Daily bars cannot express an intraday stop. The daily loss limit is checked
   at session t's open on the session that has just COMPLETED: equity at
   t-1's close against equity at t-2's close (t-1's start). So it fires one
   bar LATE. The whole of t-1's loss is taken, where a live stop would cut it
   short, and the DAY halt then covers session t (flat for t, resuming at
   t+1) rather than the rest of t-1. Real losses on the losing day would be
   smaller; what the halted session t would have earned is forgone, in
   either direction. (Before 2026-10-07 this check compared t-1's close with
   itself and so never fired; see the fix's PR.)

   The weekly loss limit is checked the same way: at t's open, over the
   ISO week that contains t-1, from that week's start (the close before its
   first session) to t-1's close. A breach completed on a week's LAST
   session is therefore found at the next week's first open, and its WEEK
   halt covers that new week -- unless a WEEK halt already covered the week
   of the loss, in which case it is not counted twice. (Before 2026-10-08
   the baseline was reset to the new week before that first check, so the
   last session of every week never counted towards any weekly loss.)

2. DAY and WEEK halts expire with their period, which assumes you resume at the
   next session. FULL halts (drawdown stop) never expire -- that is a human
   decision, so in a backtest the strategy stays flat for good, as it should.

Pass `enforce_risk=False` only to measure an unconstrained benchmark, and
label any such result clearly as not deployable.

EVENT-STAMPED SOURCES
---------------------
`sources` are frames indexed by the tz-aware instant each row became known
(e.g. weekend crypto variance). They reach the strategy only through
`view.source(name)`, bounded by `instant` = 09:30 America/New_York on the bar
date, converted to UTC -- the moment the decision is executed. A row is
visible only if it was known strictly before that open.

OPTIONAL RUN CONTROLS (all off by default; off, a run is unchanged)
-------------------------------------------------------------------
`start`: first session to trade. Earlier bars are visible to the strategy as
history but no position, cost or risk state exists before `start`. The equity
series then carries a leading point equal to `initial_equity`, stamped at the
session before `start`, so the entry cost and the first day's return are part
of every statistic computed from it.

`rebalance_dates`: sessions on which the book may trade. On any other session
positions drift with prices. Risk-layer actions still execute on any session
of an enforced run: a halt's flatten, and a change of the drawdown-warn
exposure multiplier (entering or leaving the reduction). Resuming after a
DAY or WEEK halt is not such an action; the book re-enters on the next
scheduled session.

`cash_accrual`: per-unit simple return on cash for the interval ending at each
session, (previous session close, this session close]. At each close the cash
balance as it stood at the previous close is credited with that return. The
balance is signed, so a slightly negative balance is charged at the same
rate. Before `start` (or the first bar) the previous balance is
`initial_equity`, all cash.

`cost_bps`: one-way cost in basis points PER SYMBOL, replacing limits.yaml's
single rate (see `research/costs.py` for the per-asset-class research file).
Every traded symbol must have an entry. Off, every symbol pays
`limits.costs.total_bps` exactly as before.

`fill`: "open" (the default, the convention at the top of this docstring) or
"close", a market-on-close fill:

    decide using data strictly BEFORE t  ->  execute at t's CLOSE  ->  mark at t's CLOSE

The decision is NOT moved. The strategy gets the very same view it gets in
"open" mode -- bars strictly before t, sources bounded at t's 09:30 open -- so
a bar stamped t, including the close it is filled at, stays invisible.
Nor is the ORDER: whether to trade (the rebalance band) and how many shares
are computed exactly as in "open" mode, off the account value at t's open --
the last price known before a market-on-close order must be entered. Only
the fill price moves: those shares fill at t's close. So the post-fill
weight drifts from target by the open-to-close move, as a real MOC order's
would. The risk check is unchanged too: equity at t-1's close, positions at
t's open, both known before the order is sent. A halt that fires is
therefore also flattened at t's close, carrying day t's open-to-close move
that an open-mode run would not. That move can go either way: it is NOT a
pessimistic assumption (on 2026-10-07 the turn_of_month trial's halt on
2008-12-02 gained from it).

A signal computed "just before the close" from bar t itself (IBS, say) is
therefore not expressible: bar t is complete only at the close. That needs an
intraday snapshot as an event-stamped source, a decision instant at the MOC
cutoff, and an early-close calendar (a 15:50 instant is after a 13:00 close);
none exists here, so the instant is not moved.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

import pandas as pd

from tradingbot.config import Limits, load_limits
from tradingbot.data.pit import PointInTimeView
from tradingbot.data.us_calendar import equity_open_utc
from tradingbot.research import metrics
from tradingbot.risk.engine import RiskEngine
from tradingbot.risk.state import AccountState, HaltRegistry, HaltScope, Position
from tradingbot.strategies.base import Strategy

#: Where an order fills on its session. See "fill" in the module docstring.
FILLS = ("open", "close")


@dataclass
class HaltEvent:
    timestamp: pd.Timestamp
    rule: str
    scope: str
    message: str


@dataclass
class BacktestResult:
    equity: pd.Series
    weights: pd.DataFrame
    turnover: pd.Series
    costs: pd.Series
    strategy_name: str
    initial_equity: float
    n_trials: int = 1
    risk_enforced: bool = True
    halt_events: list[HaltEvent] = field(default_factory=list)
    days_halted: int = 0
    days_reduced: int = 0
    meta: dict = field(default_factory=dict)
    #: Interest credited on cash per session; None when `cash_accrual` is off.
    interest: pd.Series | None = None

    def summary(self) -> dict[str, float]:
        stats = metrics.summary(self.equity, n_trials=self.n_trials)
        stats["days_halted"] = float(self.days_halted)
        stats["days_reduced"] = float(self.days_reduced)
        stats["halt_events"] = float(len(self.halt_events))
        return stats

    def total_costs(self) -> float:
        return float(self.costs.sum())

    def halt_report(self) -> str:
        if not self.halt_events:
            return "no halts fired"
        lines = [f"{len(self.halt_events)} halt event(s), {self.days_halted} bars flat:"]
        for event in self.halt_events[:20]:
            lines.append(f"  {event.timestamp.date()}  {event.rule:<20} -> {event.scope}")
        if len(self.halt_events) > 20:
            lines.append(f"  ... and {len(self.halt_events) - 20} more")
        return "\n".join(lines)


def build_frame(bars: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Combine per-symbol OHLCV into one wide frame of `SYMBOL_field` columns.

    Uses an inner join on the index: a backtest across instruments may only
    use days on which every instrument traded, otherwise a missing bar
    silently becomes a stale price.
    """
    frames = []
    for symbol, frame in bars.items():
        renamed = frame.copy()
        renamed.columns = [f"{symbol}_{c}" for c in renamed.columns]
        frames.append(renamed)
    combined = pd.concat(frames, axis=1, join="inner").sort_index()
    return combined.dropna()


def run(
    strategy: Strategy,
    frame: pd.DataFrame,
    symbols: list[str],
    initial_equity: float = 100_000.0,
    limits: Limits | None = None,
    n_trials: int = 1,
    enforce_risk: bool = True,
    sources: Mapping[str, pd.DataFrame] | None = None,
    start: pd.Timestamp | str | None = None,
    rebalance_dates: Collection[pd.Timestamp] | None = None,
    cash_accrual: pd.Series | None = None,
    cost_bps: Mapping[str, float] | None = None,
    fill: str = "open",
) -> BacktestResult:
    limits = limits or load_limits()
    cost_rate = limits.costs.total_bps / 10_000.0
    max_gross = limits.position.max_gross_exposure_pct / 100.0
    band = limits.position.rebalance_band_pct / 100.0

    if not isinstance(frame.index, pd.DatetimeIndex):
        raise TypeError("frame must have a DatetimeIndex")
    if not frame.index.is_monotonic_increasing:
        raise ValueError("frame index must be sorted ascending")
    if fill not in FILLS:
        raise ValueError(f"fill must be one of {FILLS}, not {fill!r}")
    symbol_rate: dict[str, float] | None = None
    if cost_bps is not None:
        missing_cost = [s for s in symbols if s not in cost_bps]
        if missing_cost:
            raise ValueError(f"cost_bps has no entry for {missing_cost}")
        if not all(math.isfinite(cost_bps[s]) and cost_bps[s] >= 0 for s in symbols):
            raise ValueError("cost_bps must be finite and not negative")
        symbol_rate = {s: float(cost_bps[s]) / 10_000.0 for s in symbols}

    engine = RiskEngine(limits=limits, halts=HaltRegistry()) if enforce_risk else None

    cash = float(initial_equity)
    shares: dict[str, float] = {s: 0.0 for s in symbols}

    first = max(strategy.warmup_periods(), 1)
    if start is not None:
        requested = int(frame.index.searchsorted(pd.Timestamp(start)))
        if requested < first:
            raise ValueError(
                f"start {pd.Timestamp(start).date()} leaves {requested} bar(s) of history; "
                f"the strategy needs at least {first}"
            )
        first = requested
    index = frame.index[first:]
    if len(index) == 0:
        raise ValueError("no bars on or after the start")

    schedule = None if rebalance_dates is None else {pd.Timestamp(d) for d in rebalance_dates}
    accrual = None
    if cash_accrual is not None:
        accrual = cash_accrual.reindex(index)
        if accrual.isna().any():
            missing = accrual.index[accrual.isna()]
            raise ValueError(
                f"cash_accrual has no value for {len(missing)} session(s), "
                f"first {missing[0].date()}"
            )
    interest_rows: list[float] = []
    cash_at_prev_close = float(initial_equity)
    last_multiplier = 1.0

    equity_rows: list[float] = []
    weight_rows: list[dict[str, float]] = []
    turnover_rows: list[float] = []
    cost_rows: list[float] = []
    halt_events: list[HaltEvent] = []

    prev_equity = float(initial_equity)
    # Equity at the start of the last completed session (the close before
    # it): the daily loss check's baseline. See modelling assumption 1.
    prev_day_start_equity = float(initial_equity)
    peak_equity = float(initial_equity)
    week_start_equity = float(initial_equity)
    prev_week: tuple[int, int] | None = None
    days_halted = 0
    days_reduced = 0

    for stamp in index:
        iso_year, iso_week, _ = stamp.isocalendar()
        # The weekly check at this open covers the session that has just
        # completed, t-1, so it is measured from the start of t-1's week.
        # Resetting the baseline before this check, as this did before,
        # dropped the last session of every week from every weekly loss.
        check_week_start = week_start_equity
        if prev_week is not None and (iso_year, iso_week) != prev_week:
            halted_last_week = engine is not None and (
                HaltScope.WEEK in engine.halts.active_scopes()
            )
            week_start_equity = prev_equity
            if engine:
                engine.halts.expire(HaltScope.WEEK)
            if halted_last_week:
                # That week's loss already halted that week: do not let the
                # same loss halt the new week as well.
                check_week_start = week_start_equity
        prev_week = (iso_year, iso_week)

        # One bar is one day. The daily loss check at this open looks at the
        # session that has just completed, t-1: its start (t-2's close)
        # against its end (t-1's close). Comparing t-1's close with itself,
        # as this did before, made the daily limit unable to fire.
        day_start_equity = prev_day_start_equity
        if engine:
            engine.halts.expire(HaltScope.DAY)

        halted = False
        multiplier = 1.0
        if engine:
            positions = {
                s: Position(s, shares[s], shares[s] * float(frame.loc[stamp, f"{s}_open"]))
                for s in symbols
                if shares[s] != 0.0
            }
            state = AccountState(
                equity=prev_equity,
                peak_equity=peak_equity,
                day_start_equity=day_start_equity,
                week_start_equity=check_week_start,
                positions=positions,
                as_of=stamp.to_pydatetime(),
            )
            for violation in engine.check_account(state):
                halt_events.append(
                    HaltEvent(
                        stamp,
                        violation.rule,
                        ",".join(sorted(s.value for s in engine.halts.active_scopes())),
                        violation.message,
                    )
                )
            halted = engine.halts.is_halted(strategy.name)

        if halted:
            targets: dict[str, float] = {}  # flatten and stay flat
            days_halted += 1
            if engine:
                # Tracked while halted too, so that resuming from a halt is
                # not mistaken for a change of the warn reduction.
                multiplier = engine.exposure_multiplier(state)
        else:
            view = PointInTimeView(
                frame,
                as_of=stamp,
                sources=sources,
                instant=equity_open_utc(stamp) if sources else None,
            )
            targets = strategy.target_weights(view)
            # Same per-instrument cap the live risk engine enforces, so a
            # backtest can never hold a position the live bot would refuse.
            cap = limits.position.max_instrument_pct / 100.0
            targets = {k: max(-cap, min(cap, v)) for k, v in targets.items()}
            gross = sum(abs(w) for w in targets.values())
            if gross > max_gross:
                scale = max_gross / gross
                targets = {k: v * scale for k, v in targets.items()}

            # Constitution III: a drawdown past the warn level reduces
            # exposure rather than halting. Pure function of state, so it
            # lifts by itself on recovery.
            if engine:
                multiplier = engine.exposure_multiplier(state)
                if multiplier != 1.0:
                    targets = {k: v * multiplier for k, v in targets.items()}
                    days_reduced += 1

        # Size today's orders off today's OPENING account value, the way a
        # broker does. Sizing off yesterday's close while valuing the position
        # at today's open manufactures drift equal to the overnight return and
        # then trades to "correct" it -- which is what made buy-and-hold pay
        # costs on nearly every bar. With fill="close" the order is still
        # sized (and banded) here, off the open: the open is the last price
        # known before a market-on-close order must be in, so the quantity
        # cannot depend on the close it fills at. Only the fill price moves.
        equity_at_open = cash + sum(
            shares[s] * float(frame.loc[stamp, f"{s}_open"]) for s in symbols
        )

        # Monthly (or any) schedule: off-schedule sessions trade only for a
        # risk-layer action. Nothing else moves the book.
        may_trade = (
            schedule is None
            or stamp in schedule
            or halted
            or (engine is not None and multiplier != last_multiplier)
        )
        if may_trade:
            last_multiplier = multiplier

        traded_notional = 0.0
        symbol_cost = 0.0
        for symbol in symbols if may_trade else ():
            open_price = float(frame.loc[stamp, f"{symbol}_open"])
            if open_price <= 0:
                continue
            target_weight = targets.get(symbol, 0.0)
            target_value = target_weight * equity_at_open
            target_shares = target_value / open_price
            delta = target_shares - shares[symbol]
            if delta == 0.0:
                continue

            # No-trade band: hold unless the weight has drifted far enough to
            # be worth the spread. A halt is exempt -- flattening must
            # complete regardless of how small the residual is.
            if not halted and equity_at_open > 0:
                current_weight = shares[symbol] * open_price / equity_at_open
                if abs(target_weight - current_weight) <= band:
                    continue
            fill_price = (
                open_price if fill == "open" else float(frame.loc[stamp, f"{symbol}_close"])
            )
            traded_notional += abs(delta * fill_price)
            if symbol_rate is not None:
                symbol_cost += abs(delta * fill_price) * symbol_rate[symbol]
            cash -= delta * fill_price
            shares[symbol] = target_shares

        cost = traded_notional * cost_rate if symbol_rate is None else symbol_cost
        cash -= cost

        if accrual is not None:
            interest = cash_at_prev_close * float(accrual.loc[stamp])
            cash += interest
            interest_rows.append(interest)
        cash_at_prev_close = cash

        mark = sum(shares[s] * float(frame.loc[stamp, f"{s}_close"]) for s in symbols)
        equity = cash + mark

        equity_rows.append(equity)
        weight_rows.append({s: targets.get(s, 0.0) for s in symbols})
        turnover_rows.append(traded_notional / equity_at_open if equity_at_open else 0.0)
        cost_rows.append(cost)

        prev_day_start_equity = prev_equity
        prev_equity = equity
        peak_equity = max(peak_equity, equity)

    # One cost key, never both: with `cost_bps` set, limits.yaml's single rate
    # was not charged, so reporting it would misstate the run.
    meta: dict = (
        {"cost_bps_one_way": limits.costs.total_bps}
        if cost_bps is None
        else {"cost_bps_by_symbol": {s: float(cost_bps[s]) for s in symbols}}
    )
    if fill != "open":
        meta["fill"] = fill

    equity_series = pd.Series(equity_rows, index=index, name="equity")
    if start is not None:
        lead = pd.Series([float(initial_equity)], index=frame.index[first - 1 : first])
        equity_series = pd.concat([lead, equity_series]).rename("equity")

    return BacktestResult(
        equity=equity_series,
        weights=pd.DataFrame(weight_rows, index=index),
        turnover=pd.Series(turnover_rows, index=index, name="turnover"),
        costs=pd.Series(cost_rows, index=index, name="costs"),
        strategy_name=strategy.name,
        initial_equity=float(initial_equity),
        n_trials=n_trials,
        risk_enforced=enforce_risk,
        halt_events=halt_events,
        days_halted=days_halted,
        days_reduced=days_reduced,
        meta=meta,
        interest=(
            pd.Series(interest_rows, index=index, name="interest") if accrual is not None else None
        ),
    )
