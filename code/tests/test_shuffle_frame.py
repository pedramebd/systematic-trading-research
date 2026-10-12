"""shuffle_frame must keep each day's intraday shape.

Regression: it used to write the rebuilt close into open, high and low as
well, so O = H = L = C on every shuffled bar. Garman-Klass and Parkinson are
functions of ln(H/L) and ln(C/O), so both were identically zero on shuffled
data and a range-based strategy was never actually put through the test.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradingbot.research.backtest import build_frame
from tradingbot.research.validation import shuffle_frame


def _bars(seed: int, n: int = 400) -> pd.DataFrame:
    """Random walk with a genuinely varying intraday shape per day."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n)))
    open_ = close * np.exp(rng.normal(0.0, 0.006, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0.0, 0.005, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0.0, 0.005, n)))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": 1e6}, index=idx
    )


@pytest.fixture
def shaped_frame() -> pd.DataFrame:
    return build_frame({"SPY": _bars(1), "GLD": _bars(2)})


def _garman_klass(frame: pd.DataFrame, symbol: str) -> np.ndarray:
    o, h, lo, c = (frame[f"{symbol}_{f}"].to_numpy() for f in ("open", "high", "low", "close"))
    return 0.5 * np.log(h / lo) ** 2 - (2 * np.log(2) - 1) * np.log(c / o) ** 2


def _parkinson(frame: pd.DataFrame, symbol: str) -> np.ndarray:
    h, lo = frame[f"{symbol}_high"].to_numpy(), frame[f"{symbol}_low"].to_numpy()
    return np.log(h / lo) ** 2 / (4 * np.log(2))


def _legacy_close(frame: pd.DataFrame, symbols: list[str], seed: int) -> dict[str, np.ndarray]:
    """The close path exactly as the old implementation built it."""
    rng = np.random.default_rng(seed)
    out = {}
    for symbol in symbols:
        close = frame[f"{symbol}_close"]
        returns = np.array(close.pct_change().dropna().to_numpy(), dtype=float, copy=True)
        rng.shuffle(returns)
        out[symbol] = float(close.iloc[0]) * np.cumprod(np.concatenate([[1.0], 1.0 + returns]))
    return out


@pytest.mark.parametrize("estimator", [_garman_klass, _parkinson], ids=["gk", "parkinson"])
def test_range_estimators_survive_the_shuffle(shaped_frame, estimator):
    """Each day's estimator value is scale-free, so the shuffle must permute
    them, not zero them."""
    shuffled = shuffle_frame(shaped_frame, ["SPY", "GLD"], seed=0)
    for symbol in ("SPY", "GLD"):
        before = estimator(shaped_frame, symbol)
        after = estimator(shuffled, symbol)
        assert np.all(after > 0), "range estimator collapsed on shuffled data"
        np.testing.assert_allclose(np.sort(after), np.sort(before), rtol=1e-9)
        assert not np.allclose(after, before), "the serial order was not changed"


def test_each_day_keeps_its_return_and_shape_together(shaped_frame):
    """The joint distribution of (return, O/C, H/C, L/C) is preserved: every
    shuffled day is some original day, whole."""

    def rows(frame: pd.DataFrame) -> np.ndarray:
        c = frame["SPY_close"].to_numpy()
        ret = c[1:] / c[:-1] - 1.0
        shape = [frame[f"SPY_{f}"].to_numpy()[1:] / c[1:] for f in ("open", "high", "low")]
        return np.column_stack([ret, *shape])

    before = rows(shaped_frame)
    after = rows(shuffle_frame(shaped_frame, ["SPY"], seed=3))
    order = np.lexsort(before.T[::-1])
    order_after = np.lexsort(after.T[::-1])
    np.testing.assert_allclose(after[order_after], before[order], rtol=1e-9, atol=1e-12)


def test_shuffled_bars_are_internally_consistent(shaped_frame):
    shuffled = shuffle_frame(shaped_frame, ["SPY", "GLD"], seed=7)
    for symbol in ("SPY", "GLD"):
        o, h, lo, c = (shuffled[f"{symbol}_{f}"] for f in ("open", "high", "low", "close"))
        assert (h >= np.maximum(o, c) * (1 - 1e-12)).all()
        assert (lo <= np.minimum(o, c) * (1 + 1e-12)).all()
        assert (lo > 0).all()


def test_first_bar_is_untouched(shaped_frame):
    shuffled = shuffle_frame(shaped_frame, ["SPY"], seed=5)
    pd.testing.assert_series_equal(shuffled.iloc[0], shaped_frame.iloc[0])


def test_close_path_is_unchanged_from_the_old_implementation(shaped_frame):
    """Close-only results recorded before this fix must reproduce."""
    for seed in (0, 1, 9):
        shuffled = shuffle_frame(shaped_frame, ["SPY", "GLD"], seed=seed)
        legacy = _legacy_close(shaped_frame, ["SPY", "GLD"], seed=seed)
        for symbol in ("SPY", "GLD"):
            np.testing.assert_array_equal(shuffled[f"{symbol}_close"].to_numpy(), legacy[symbol])


def test_volume_and_unlisted_symbols_pass_through(shaped_frame):
    shuffled = shuffle_frame(shaped_frame, ["SPY"], seed=2)
    pd.testing.assert_series_equal(shuffled["SPY_volume"], shaped_frame["SPY_volume"])
    gld = [c for c in shaped_frame.columns if c.startswith("GLD_")]
    pd.testing.assert_frame_equal(shuffled[gld], shaped_frame[gld])


# --- joint permutation (trial 3, criterion 4a) ---------------------------------


def _daily_rows(frame: pd.DataFrame) -> np.ndarray:
    """(SPY return, GLD return, SPY H/C, GLD H/C) per day from day 1."""
    cols = []
    for symbol in ("SPY", "GLD"):
        c = frame[f"{symbol}_close"].to_numpy()
        cols.append(c[1:] / c[:-1] - 1.0)
    for symbol in ("SPY", "GLD"):
        c = frame[f"{symbol}_close"].to_numpy()
        cols.append(frame[f"{symbol}_high"].to_numpy()[1:] / c[1:])
    return np.column_stack(cols)


def test_joint_shuffle_keeps_each_day_across_symbols_together(shaped_frame):
    before = _daily_rows(shaped_frame)
    after = _daily_rows(shuffle_frame(shaped_frame, ["SPY", "GLD"], seed=4, joint=True))
    order_before = np.lexsort(before.T[::-1])
    order_after = np.lexsort(after.T[::-1])
    np.testing.assert_allclose(after[order_after], before[order_before], rtol=1e-9, atol=1e-12)
    assert not np.allclose(after, before), "the serial order was not changed"


def test_joint_shuffle_preserves_cross_correlation_independent_does_not(shaped_frame):
    frame = shaped_frame.copy()
    # make GLD strongly correlated with SPY, so the difference is visible
    spy_ret = frame["SPY_close"].pct_change().fillna(0.0).to_numpy()
    gld_close = 80.0 * np.cumprod(1.0 + 0.9 * spy_ret)
    for field, ratio in (("open", 1.0), ("high", 1.004), ("low", 0.996), ("close", 1.0)):
        frame[f"GLD_{field}"] = gld_close * ratio

    def corr(f: pd.DataFrame) -> float:
        return float(np.corrcoef(_daily_rows(f)[:, 0], _daily_rows(f)[:, 1])[0, 1])

    joint = corr(shuffle_frame(frame, ["SPY", "GLD"], seed=0, joint=True))
    independent = corr(shuffle_frame(frame, ["SPY", "GLD"], seed=0))
    assert joint == pytest.approx(corr(frame), abs=1e-9)
    assert abs(independent) < 0.2


def test_joint_off_is_the_default(shaped_frame):
    pd.testing.assert_frame_equal(
        shuffle_frame(shaped_frame, ["SPY", "GLD"], seed=3),
        shuffle_frame(shaped_frame, ["SPY", "GLD"], seed=3, joint=False),
    )


# --- shuffle_test options -------------------------------------------------------


def test_shuffle_test_default_control_is_first_symbol_buy_and_hold(shaped_frame):
    from tradingbot.research import metrics
    from tradingbot.research.backtest import run
    from tradingbot.research.validation import shuffle_test
    from tradingbot.strategies.buy_and_hold import BuyAndHold

    result = shuffle_test(lambda _f: BuyAndHold("GLD"), shaped_frame, ["SPY", "GLD"], n_runs=2)
    fake = shuffle_frame(shaped_frame, ["SPY", "GLD"], seed=1)
    expected = run(BuyAndHold("SPY"), fake, ["SPY", "GLD"], enforce_risk=False)
    assert result.control_sharpes[1] == metrics.sharpe(expected.equity.pct_change().dropna())


def test_shuffle_test_uses_a_supplied_control_and_run_kwargs(shaped_frame):
    from tradingbot.research import metrics
    from tradingbot.research.backtest import run
    from tradingbot.research.validation import shuffle_test
    from tradingbot.strategies.buy_and_hold import BuyAndHold
    from tradingbot.strategies.spy_gld_5050 import SpyGld5050

    kwargs = {"start": shaped_frame.index[20], "rebalance_dates": shaped_frame.index[20::21]}
    result = shuffle_test(
        lambda _f: BuyAndHold("SPY"),
        shaped_frame,
        ["SPY", "GLD"],
        n_runs=2,
        seed=5,
        control=lambda _f: SpyGld5050(),
        joint=True,
        run_kwargs=kwargs,
    )
    fake = shuffle_frame(shaped_frame, ["SPY", "GLD"], seed=6, joint=True)
    expected = run(SpyGld5050(), fake, ["SPY", "GLD"], enforce_risk=False, **kwargs)
    assert result.control_sharpes[1] == metrics.sharpe(expected.equity.pct_change().dropna())
    assert len(expected.equity) == len(shaped_frame) - 19  # start honoured, leading point kept


def test_shuffle_test_refuses_to_override_its_own_run_settings(shaped_frame):
    from tradingbot.research.validation import shuffle_test
    from tradingbot.strategies.buy_and_hold import BuyAndHold

    with pytest.raises(ValueError, match="enforce_risk"):
        shuffle_test(
            lambda _f: BuyAndHold("SPY"),
            shaped_frame,
            ["SPY"],
            n_runs=1,
            run_kwargs={"enforce_risk": True},
        )
