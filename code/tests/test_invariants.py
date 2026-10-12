"""Trading-specific invariants.

These catch the bugs that cost money. No linter finds any of them.
"""

import numpy as np
import pandas as pd
import pytest

from tradingbot.data.pit import LookAheadError, PointInTimeView
from tradingbot.research import metrics
from tradingbot.research.backtest import run
from tradingbot.research.validation import determinism_check, shuffle_test
from tradingbot.strategies.base import Strategy
from tradingbot.strategies.buy_and_hold import BuyAndHold, ZeroSignal


class PeekingStrategy(Strategy):
    """Tries to read tomorrow through the view. Must be stopped."""

    name = "peeking"

    def target_weights(self, view: PointInTimeView):
        future = view.as_of + pd.Timedelta(days=1)
        return {"SPY": 1.0 if view.at("SPY_close", future) > 0 else 0.0}


class LeakyOracle(Strategy):
    """Bypasses the view entirely by closing over the raw frame -- the way
    look-ahead actually gets written in real life. Used to prove the shuffle
    test detects leakage."""

    name = "leaky_oracle"

    def __init__(self, frame: pd.DataFrame):
        self._frame = frame

    def target_weights(self, view: PointInTimeView):
        idx = self._frame.index
        pos = idx.searchsorted(view.as_of)
        if pos + 1 >= len(idx):
            return {}
        today = float(self._frame.iloc[pos]["SPY_close"])
        tomorrow = float(self._frame.iloc[pos + 1]["SPY_close"])
        return {"SPY": 1.0 if tomorrow > today else 0.0}


def test_zero_signal_produces_exactly_zero_pnl(synthetic_frame):
    """If doing nothing moves the equity curve, the accounting is wrong."""
    result = run(ZeroSignal(), synthetic_frame, ["SPY"], initial_equity=100_000.0)
    assert result.equity.nunique() == 1
    assert result.equity.iloc[-1] == pytest.approx(100_000.0)
    assert result.total_costs() == 0.0


def test_peeking_through_the_view_raises(synthetic_frame):
    with pytest.raises(LookAheadError):
        run(PeekingStrategy(), synthetic_frame, ["SPY"])


def test_backtest_is_deterministic(synthetic_frame):
    assert determinism_check(lambda _: BuyAndHold("SPY"), synthetic_frame, ["SPY"])


def test_shuffle_test_detects_leakage(synthetic_frame):
    """An oracle that sees tomorrow keeps its edge on shuffled data. That is
    the signature the shuffle test exists to catch."""
    result = shuffle_test(LeakyOracle, synthetic_frame, ["SPY"], n_runs=3)
    assert result.observed_sharpe > 2.0, "an oracle should look absurdly good"
    assert result.suspicious, result.report()


def test_costs_are_actually_charged(synthetic_frame):
    """Buy-and-hold pays cost once, at entry. If costs are zero, the cost
    model is not wired in."""
    result = run(BuyAndHold("SPY"), synthetic_frame, ["SPY"], initial_equity=100_000.0)
    assert result.total_costs() > 0.0


def test_equity_never_goes_negative_without_leverage(synthetic_frame):
    result = run(BuyAndHold("SPY"), synthetic_frame, ["SPY"])
    assert (result.equity > 0).all()


def test_gross_exposure_is_capped(synthetic_frame):
    class Greedy(Strategy):
        name = "greedy"

        def target_weights(self, view):
            return {"SPY": 5.0}

    result = run(Greedy(), synthetic_frame, ["SPY"])
    assert result.weights["SPY"].max() <= 1.0 + 1e-9


def test_sharpe_of_zero_variance_series_is_zero():
    flat = pd.Series(np.zeros(100))
    assert metrics.sharpe(flat) == 0.0


def test_shuffle_frame_handles_read_only_arrays(synthetic_frame):
    """Regression: pandas can hand back a read-only numpy view, and shuffling
    mutates in place. This failed on Windows/numpy while passing on Linux."""
    from tradingbot.research.validation import shuffle_frame

    frame = synthetic_frame.copy()
    for col in frame.columns:
        frame[col].to_numpy().flags.writeable = False

    shuffled = shuffle_frame(frame, ["SPY"], seed=1)
    assert len(shuffled) == len(frame)
    assert not shuffled["SPY_close"].equals(frame["SPY_close"])
