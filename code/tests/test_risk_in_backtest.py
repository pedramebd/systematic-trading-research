"""The backtest must honour the constitution.

An unconstrained backtest flatters a strategy the live system would have
halted. These tests prove enforcement is actually wired in, not merely
importable.
"""

import numpy as np
import pandas as pd
import pytest

from tradingbot.config import load_limits
from tradingbot.research.backtest import build_frame, run
from tradingbot.risk.engine import RiskEngine
from tradingbot.risk.state import HaltRegistry, HaltScope
from tradingbot.strategies.buy_and_hold import BuyAndHold


@pytest.fixture
def crashing_frame() -> pd.DataFrame:
    """Flat, then a sustained crash deep enough to breach the drawdown stop."""
    n_calm, n_crash = 60, 60
    calm = np.full(n_calm, 100.0)
    crash = 100.0 * np.exp(np.cumsum(np.full(n_crash, -0.006)))
    close = np.concatenate([calm, crash])
    idx = pd.date_range("2022-01-03", periods=len(close), freq="B")
    bars = pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1_000_000.0},
        index=idx,
    )
    return build_frame({"SPY": bars})


def test_drawdown_stop_fires_and_flattens(crashing_frame):
    result = run(BuyAndHold("SPY"), crashing_frame, ["SPY"], enforce_risk=True)
    assert result.halt_events, "a 30% crash must trigger halts"
    assert result.days_halted > 0
    assert result.weights["SPY"].iloc[-1] == 0.0, "must be flat after a full halt"


def test_enforcement_limits_the_drawdown(crashing_frame):
    enforced = run(BuyAndHold("SPY"), crashing_frame, ["SPY"], enforce_risk=True)
    naked = run(BuyAndHold("SPY"), crashing_frame, ["SPY"], enforce_risk=False)
    assert enforced.summary()["max_drawdown"] < naked.summary()["max_drawdown"]


def test_drawdown_stop_never_auto_resumes(crashing_frame):
    """A FULL halt is a human decision. It must not lift on its own."""
    result = run(BuyAndHold("SPY"), crashing_frame, ["SPY"], enforce_risk=True)
    tail = result.weights["SPY"].iloc[-10:]
    assert (tail == 0.0).all(), "full halt lifted itself -- constitution V breach"


def test_enforcement_is_on_by_default(synthetic_frame):
    assert run(BuyAndHold("SPY"), synthetic_frame, ["SPY"]).risk_enforced is True


def test_unconstrained_run_is_labelled(synthetic_frame):
    assert (
        run(BuyAndHold("SPY"), synthetic_frame, ["SPY"], enforce_risk=False).risk_enforced is False
    )


def test_quiet_market_triggers_no_halts(monkeypatch):
    """Enforcement must not fire spuriously on ordinary conditions.

    The path is GENUINELY quiet: no daily loss reaches the daily limit and the
    drawdown stays well under the warn level (both asserted, so the test cannot
    pass vacuously). And the check must actually run, on every bar, with real
    (non-zero) day losses -- the daily check once saw a constant 0."""
    rng = np.random.default_rng(2024)
    n = 750
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.003, n)))
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    bars = pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1e6}, index=idx
    )
    frame = build_frame({"SPY": bars})
    limits = load_limits()
    daily = frame["SPY_close"].pct_change().dropna()
    assert -daily.min() * 100 < limits.loss.daily_loss_pct / 2
    drawdown = 1 - (frame["SPY_close"] / frame["SPY_close"].cummax()).min()
    assert drawdown * 100 < limits.loss.drawdown_warn_pct / 2

    seen: list[float] = []
    original = RiskEngine.check_account

    def counting(self, state):
        seen.append(state.day_loss_pct())
        return original(self, state)

    monkeypatch.setattr(RiskEngine, "check_account", counting)
    result = run(BuyAndHold("SPY"), frame, ["SPY"], enforce_risk=True)
    assert len(seen) == len(result.equity), "the account check must run on every bar"
    assert max(seen) > 0.0, "the daily check must see real day losses, not a constant 0"
    assert not result.halt_events
    assert result.days_reduced == 0


def test_week_halts_expire_but_full_halts_do_not():
    halts = HaltRegistry()
    with pytest.raises(ValueError):
        halts.expire(HaltScope.FULL)


def test_an_active_halt_does_not_re_register_every_bar(crashing_frame):
    """Regression: a full halt used to emit one event per remaining bar,
    which in live trading is one alert per cycle, forever."""
    result = run(BuyAndHold("SPY"), crashing_frame, ["SPY"], enforce_risk=True)
    scopes = [e.scope for e in result.halt_events]
    assert len(result.halt_events) < 10, (
        f"{len(result.halt_events)} halt events for one crash: {scopes[:5]}"
    )
    assert result.days_halted > len(result.halt_events), (
        "should stay halted for many bars while emitting few events"
    )


# ---- daily loss limit (fix: it used to compare the previous close with itself) ----


def _one_bad_day(drop: float) -> tuple[pd.DataFrame, pd.Timestamp]:
    """Flat at 100, one session closes `drop` lower (opening at 100), then flat
    again at the new level. Monday 2024-01-08 start; the loss is on Wednesday,
    so the week, drawdown and daily limits can be told apart."""
    idx = pd.bdate_range("2024-01-08", periods=10)
    close = np.full(len(idx), 100.0)
    open_ = close.copy()
    bad = 2
    close[bad:] = 100.0 * (1.0 - drop)
    open_[bad + 1 :] = 100.0 * (1.0 - drop)
    bars = pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close),
            "low": np.minimum(open_, close),
            "close": close,
            "volume": 1e6,
        },
        index=idx,
    )
    return build_frame({"SPY": bars}), idx[bad]


def test_daily_loss_fires_one_bar_late_and_flattens_for_one_session():
    """A 3% loss on one session (daily limit 2%, weekly 5%, warn 10%) halts
    the NEXT session -- daily bars cannot stop intraday -- and only that one."""
    limits = load_limits()
    assert limits.loss.daily_loss_pct < 3.0 < limits.loss.weekly_loss_pct
    frame, bad_day = _one_bad_day(0.03)
    result = run(BuyAndHold("SPY"), frame, ["SPY"], enforce_risk=True)
    next_day = frame.index[frame.index.get_loc(bad_day) + 1]
    rules = [(e.timestamp, e.rule) for e in result.halt_events]
    assert rules == [(next_day, "daily_loss")], rules
    assert result.weights.loc[next_day, "SPY"] == 0.0, "the halted session must be flat"
    after = frame.index[frame.index.get_loc(next_day) + 1]
    assert result.weights.loc[after, "SPY"] == 1.0, "a DAY halt lifts the session after"
    assert result.days_halted == 1


def test_loss_below_the_daily_limit_does_not_halt():
    frame, _ = _one_bad_day(0.015)
    result = run(BuyAndHold("SPY"), frame, ["SPY"], enforce_risk=True)
    assert not result.halt_events


def test_daily_loss_is_measured_over_the_completed_session_only():
    """Two 1.5% losses on consecutive sessions are 3% over two days but never
    2% in one: no daily halt."""
    idx = pd.bdate_range("2024-01-08", periods=8)
    close = np.array([100, 100, 98.5, 97.0225, 97.0225, 97.0225, 97.0225, 97.0225])
    open_ = np.concatenate([[100.0], close[:-1]])
    bars = pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close),
            "low": np.minimum(open_, close),
            "close": close,
            "volume": 1e6,
        },
        index=idx,
    )
    result = run(BuyAndHold("SPY"), build_frame({"SPY": bars}), ["SPY"], enforce_risk=True)
    assert "daily_loss" not in [e.rule for e in result.halt_events]


def test_close_fill_flatten_carries_the_halted_sessions_move_either_way():
    """With fill="close" a halt decided at t's open flattens at t's CLOSE, so
    the book carries t's move -- favourable or not. Here t rallies 4%: the
    halted session gains, which an open-fill flatten would not have."""
    frame, bad_day = _one_bad_day(0.03)
    next_day = frame.index[frame.index.get_loc(bad_day) + 1]
    frame.loc[next_day:, "SPY_close"] *= 1.04
    frame.loc[frame.index > next_day, "SPY_open"] *= 1.04
    by_fill = {
        fill: run(BuyAndHold("SPY"), frame, ["SPY"], enforce_risk=True, fill=fill)
        for fill in ("open", "close")
    }
    for result in by_fill.values():
        assert (next_day, "daily_loss") in [(e.timestamp, e.rule) for e in result.halt_events]
    gain = {f: r.equity.loc[next_day] / r.equity.loc[bad_day] - 1 for f, r in by_fill.items()}
    # Open fill: flat from the open, so only the 6 bp exit cost.
    assert gain["open"] == pytest.approx(-0.0006, abs=1e-5)
    assert gain["close"] > 0.03


# ---- weekly loss limit (fix: a week's last session never counted) ----


def _weekly_frame(daily_moves: dict[str, float]) -> pd.DataFrame:
    """Flat at 100 from Monday 2024-01-01 for four weeks, except the given
    sessions, each of which opens at the previous close and closes `move`
    away from it."""
    idx = pd.bdate_range("2024-01-01", "2024-01-26")
    close = []
    level = 100.0
    for day in idx:
        level *= 1.0 + daily_moves.get(str(day.date()), 0.0)
        close.append(level)
    close = np.array(close)
    open_ = np.concatenate([[100.0], close[:-1]])
    bars = pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close),
            "low": np.minimum(open_, close),
            "close": close,
            "volume": 1e6,
        },
        index=idx,
    )
    return build_frame({"SPY": bars})


def test_weekly_loss_completed_on_the_weeks_last_session_is_counted():
    """Five 1.1% losses, Monday to Friday of the second week: never a 2% day,
    under 5% through Thursday, past 5% only with Friday. The old code reset
    the week before checking it and never saw the breach."""
    loss = {
        d: -0.011 for d in ("2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11", "2024-01-12")
    }
    frame = _weekly_frame(loss)
    limits = load_limits()
    thursday = frame.loc["2024-01-11", "SPY_close"] / 100.0 - 1
    friday = frame.loc["2024-01-12", "SPY_close"] / 100.0 - 1
    assert -thursday * 100 < limits.loss.weekly_loss_pct < -friday * 100
    result = run(BuyAndHold("SPY"), frame, ["SPY"], enforce_risk=True)
    rules = [(e.timestamp, e.rule) for e in result.halt_events]
    assert rules == [(pd.Timestamp("2024-01-15"), "weekly_loss")], rules
    halted_week = result.weights.loc["2024-01-15":"2024-01-19", "SPY"]
    assert (halted_week == 0.0).all(), "the WEEK halt found on Monday covers that week"
    assert result.weights.loc["2024-01-22", "SPY"] == 1.0, "and lifts the week after"


def test_a_weekly_loss_already_halted_is_not_counted_twice():
    """Three 1.8% losses Monday to Wednesday breach 5% at Wednesday's close:
    halted Thursday and Friday. That loss must not halt the next week too."""
    loss = {d: -0.018 for d in ("2024-01-08", "2024-01-09", "2024-01-10")}
    result = run(BuyAndHold("SPY"), _weekly_frame(loss), ["SPY"], enforce_risk=True)
    weekly = [e.timestamp for e in result.halt_events if e.rule == "weekly_loss"]
    assert weekly == [pd.Timestamp("2024-01-11")], weekly
    assert (result.weights.loc["2024-01-11":"2024-01-12", "SPY"] == 0.0).all()
    assert result.weights.loc["2024-01-15", "SPY"] == 1.0
