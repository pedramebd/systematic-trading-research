"""trend_spy_gld (pre-registration section 6) and its control."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradingbot.data.pit import PointInTimeView
from tradingbot.research.backtest import build_frame, run
from tradingbot.research.validation import determinism_check
from tradingbot.strategies.spy_gld_5050 import SpyGld5050
from tradingbot.strategies.trend_spy_gld import TrendSpyGld


def _bars(close: np.ndarray, index: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(
        {"open": close, "high": close * 1.001, "low": close * 0.999, "close": close, "volume": 1e6},
        index=index,
    )


def _frame(spy: np.ndarray, gld: np.ndarray, start: str = "2005-01-03") -> pd.DataFrame:
    index = pd.bdate_range(start, periods=len(spy))
    return build_frame({"SPY": _bars(spy, index), "GLD": _bars(gld, index)})


@pytest.fixture
def walk_frame() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    n = 1300  # ~5 years of business days
    spy = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n)))
    gld = 80 * np.exp(np.cumsum(rng.normal(0.0002, 0.010, n)))
    return _frame(spy, gld)


def _first_sessions(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    month = index.year * 12 + index.month
    return index[np.append(True, month[1:] != month[:-1])]


def test_month_ends_exclude_the_decision_month(walk_frame):
    strategy = TrendSpyGld()
    mid_month = walk_frame.index[walk_frame.index.month == 3][10]  # a mid-March session
    view = PointInTimeView(walk_frame, as_of=mid_month)
    got = strategy.month_end_closes(view, "SPY")
    visible = walk_frame.loc[walk_frame.index < mid_month, "SPY_close"]
    expected = visible.groupby(visible.index.to_period("M")).last()
    expected = expected[expected.index < mid_month.to_period("M")]
    np.testing.assert_array_equal(got, expected.to_numpy())


def test_first_session_of_month_sees_the_previous_month_end(walk_frame):
    """The switch executes the same morning, not a session late."""
    strategy = TrendSpyGld()
    first = _first_sessions(walk_frame.index)[5]
    previous_bar = walk_frame.index[walk_frame.index.get_loc(first) - 1]
    got = strategy.month_end_closes(PointInTimeView(walk_frame, as_of=first), "GLD")
    assert got[-1] == walk_frame.loc[previous_bar, "GLD_close"]


def test_signal_matches_an_independent_monthly_computation(walk_frame):
    """Rule re-derived with pandas resampling, checked on every first session."""
    strategy = TrendSpyGld()
    for symbol in ("SPY", "GLD"):
        month_end = walk_frame[f"{symbol}_close"].resample("ME").last()
        sma = month_end.rolling(10).mean()
        invested = (month_end > sma) & sma.notna()
        for first in _first_sessions(walk_frame.index)[1:]:
            prior_month = first.to_period("M") - 1
            expected = bool(invested[invested.index.to_period("M") == prior_month].iloc[0])
            weights = strategy.target_weights(PointInTimeView(walk_frame, as_of=first))
            assert weights[symbol] == (0.5 if expected else 0.0), (symbol, first)


def test_rising_invested_falling_cash_tie_cash():
    n = 400
    rising = np.linspace(100, 200, n)
    falling = np.linspace(200, 100, n)
    frame = _frame(rising, falling)
    weights = TrendSpyGld().target_weights(PointInTimeView(frame, as_of=frame.index[-1]))
    assert weights == {"SPY": 0.5, "GLD": 0.0}

    flat = _frame(np.full(n, 100.0), np.full(n, 100.0))
    weights = TrendSpyGld().target_weights(PointInTimeView(flat, as_of=flat.index[-1]))
    assert weights == {"SPY": 0.0, "GLD": 0.0}, "P == SMA is cash (strict >)"


def test_fewer_than_ten_month_ends_is_cash():
    frame = _frame(np.linspace(100, 200, 180), np.linspace(100, 200, 180))  # ~8.5 months
    weights = TrendSpyGld().target_weights(PointInTimeView(frame, as_of=frame.index[-1]))
    assert weights == {"SPY": 0.0, "GLD": 0.0}


def test_decision_ignores_everything_from_the_decision_session_on(walk_frame):
    strategy = TrendSpyGld()
    as_of = _first_sessions(walk_frame.index)[20]
    tampered = walk_frame.copy()
    tampered.loc[tampered.index >= as_of, ["SPY_close", "GLD_close"]] *= 10.0
    before = strategy.target_weights(PointInTimeView(walk_frame, as_of=as_of))
    after = strategy.target_weights(PointInTimeView(tampered, as_of=as_of))
    assert before == after


def test_target_is_constant_within_a_month(walk_frame):
    strategy = TrendSpyGld()
    month = walk_frame.index[(walk_frame.index.year == 2007) & (walk_frame.index.month == 6)]
    decisions = {
        tuple(sorted(strategy.target_weights(PointInTimeView(walk_frame, as_of=d)).items()))
        for d in month
    }
    assert len(decisions) == 1


def test_monthly_schedule_trades_only_on_first_sessions(walk_frame):
    schedule = _first_sessions(walk_frame.index)
    start = schedule[schedule >= pd.Timestamp("2006-01-01")][0]
    for strategy in (TrendSpyGld(), SpyGld5050()):
        result = run(
            strategy,
            walk_frame,
            ["SPY", "GLD"],
            enforce_risk=False,
            start=start,
            rebalance_dates=schedule,
        )
        traded = result.costs.index[result.costs > 0]
        assert len(traded) > 0
        assert set(traded) <= set(schedule), strategy.name


def test_control_holds_fifty_fifty():
    frame = _frame(np.linspace(100, 110, 50), np.linspace(50, 40, 50))
    assert SpyGld5050().target_weights(PointInTimeView(frame, as_of=frame.index[10])) == {
        "SPY": 0.5,
        "GLD": 0.5,
    }
    assert SpyGld5050().target_weights(PointInTimeView(frame, as_of=frame.index[0])) == {}


def test_strategy_is_deterministic(walk_frame):
    assert determinism_check(lambda _: TrendSpyGld(), walk_frame, ["SPY", "GLD"])
