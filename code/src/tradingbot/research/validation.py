"""Adversarial checks. The job of this module is to kill strategies.

The shuffle test deserves an explanation, because it is easy to misread.

Shuffling returns destroys serial structure while preserving the return
distribution. A timing strategy whose edge is real depends on that structure,
so its Sharpe should collapse towards zero on shuffled data. If it does NOT
collapse, the strategy is making money from something other than genuine
temporal predictability -- most often leakage of future information.

The test therefore does not apply to buy-and-hold, which has no timing
component: its edge is drift, and drift survives shuffling by construction.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from tradingbot.research import metrics
from tradingbot.research.backtest import run
from tradingbot.strategies.base import Strategy
from tradingbot.strategies.buy_and_hold import BuyAndHold

#: A factory receives the frame it will be run on. This matters: a
#: strategy that leaks future information must leak from the data it is
#: actually given, otherwise the shuffle test cannot detect the leak.
StrategyFactory = Callable[[pd.DataFrame], Strategy]


@dataclass
class ShuffleTestResult:
    observed_sharpe: float
    shuffled_sharpes: list[float]
    control_sharpes: list[float]
    suspicious: bool
    threshold: float

    @property
    def mean_shuffled(self) -> float:
        return float(np.mean(self.shuffled_sharpes)) if self.shuffled_sharpes else 0.0

    @property
    def mean_control(self) -> float:
        return float(np.mean(self.control_sharpes)) if self.control_sharpes else 0.0

    @property
    def excess_per_run(self) -> np.ndarray:
        return np.asarray(self.shuffled_sharpes, dtype=float) - np.asarray(
            self.control_sharpes, dtype=float
        )

    @property
    def mean_excess(self) -> float:
        """Shuffled Sharpe ABOVE a buy-and-hold control on the same shuffled
        prices. This, not the raw shuffled Sharpe, is the leakage signal."""
        return self.mean_shuffled - self.mean_control

    @property
    def t_stat(self) -> float:
        """Mean excess in units of its own standard error.

        Excess Sharpe across shuffles is noisy -- a spread of ~0.9 per run is
        normal -- so a mean above the threshold proves nothing on its own.
        Ten runs of that spread give a standard error of ~0.29, which a single
        unlucky draw clears easily.
        """
        excess = self.excess_per_run
        if len(excess) < 2:
            return 0.0
        sd = float(np.std(excess, ddof=1))
        if sd == 0.0:
            return float("inf") if self.mean_excess > 0 else 0.0
        return float(self.mean_excess / (sd / np.sqrt(len(excess))))

    def report(self) -> str:
        verdict = "SUSPICIOUS -- probable look-ahead" if self.suspicious else "ok"
        return (
            f"observed Sharpe {self.observed_sharpe:.3f}; "
            f"mean shuffled {self.mean_shuffled:.3f} vs control {self.mean_control:.3f} "
            f"= excess {self.mean_excess:.3f} (t={self.t_stat:.2f}) "
            f"over {len(self.shuffled_sharpes)} runs; {verdict}"
        )


def shuffle_frame(
    frame: pd.DataFrame, symbols: list[str], seed: int, joint: bool = False
) -> pd.DataFrame:
    """Rebuild prices from randomly permuted daily returns.

    Each day is permuted as a unit: its close-to-close return AND its intraday
    shape -- open, high and low as ratios of that day's close -- move together.
    The rebuilt close is the permuted returns compounded from the first close;
    the rebuilt open, high and low are that close times the day's own ratios.

    Writing the rebuilt close into open, high and low as well (the previous
    behaviour) made O = H = L = C, so every range estimator -- Garman-Klass,
    Parkinson -- was identically zero on shuffled data, and a strategy built
    on one was not tested at all. Keeping the shape with its return also
    keeps the joint distribution of return and range, while the serial
    structure of both is destroyed, which is the point of the test.

    The close path is identical to the previous implementation for the same
    seed, so close-only results from earlier runs are unchanged.

    `joint=False` (the default) draws a separate permutation per symbol, which
    also destroys the correlation between symbols. `joint=True` draws ONE
    permutation of days and applies it to every symbol, so each day's returns
    across symbols stay together: cross-sectional correlation survives and
    only serial structure is destroyed. The symbols share the frame's index,
    so the permutation is well defined.
    """
    rng = np.random.default_rng(seed)
    out = frame.copy()
    shared_order = rng.permutation(len(frame) - 1) if joint else None
    for symbol in symbols:
        close = frame[f"{symbol}_close"]
        # np.array(..., copy=True) matters: on newer numpy/pandas, to_numpy()
        # can return a read-only view. Indexing below does not mutate, but
        # the copy keeps this safe if that ever changes.
        closes = np.array(close.to_numpy(), dtype=float, copy=True)
        returns = closes[1:] / closes[:-1] - 1.0
        # permutation(n) applies the same swaps shuffle() would to an array of
        # length n, so returns[order] equals the old in-place shuffle exactly.
        order = shared_order if joint else rng.permutation(len(returns))
        rebuilt = closes[0] * np.cumprod(np.concatenate([[1.0], 1.0 + returns[order]]))
        # Day 0 has no return to permute; it keeps its own shape. Day i >= 1
        # takes the shape of the day whose return it now carries.
        source_day = np.concatenate([[0], order + 1])
        for field in ("open", "high", "low"):
            col = f"{symbol}_{field}"
            if col in out.columns:
                ratio = np.asarray(frame[col].to_numpy(), dtype=float) / closes
                out[col] = rebuilt * ratio[source_day]
        out[f"{symbol}_close"] = rebuilt
    return out


def shuffle_test(
    factory: StrategyFactory,
    frame: pd.DataFrame,
    symbols: list[str],
    n_runs: int = 10,
    seed: int = 0,
    threshold: float = 0.5,
    t_threshold: float = 2.0,
    sources: Mapping[str, pd.DataFrame] | None = None,
    control: StrategyFactory | None = None,
    joint: bool = False,
    run_kwargs: Mapping[str, object] | None = None,
) -> ShuffleTestResult:
    """Run the strategy on real data, then on shuffled data `n_runs` times.

    The verdict is the strategy's shuffled Sharpe MINUS a buy-and-hold
    control's Sharpe on exactly the same shuffled prices. Comparing against a
    fixed absolute threshold flagged any long-only strategy on an asset that
    drifts upward -- including buy-and-hold itself, which cannot leak. What
    matters is whether the strategy keeps an edge OVER simply holding, once
    the serial structure it claims to exploit has been destroyed.

    `sources` (event-stamped side data, see backtest.run) are passed through
    unshuffled: only the traded symbols' returns are permuted. A strategy
    whose signal lives entirely in `sources` is therefore NOT fully covered by
    this test -- say so when reporting one.

    Every run here is UNENFORCED (enforce_risk=False), strategy and control
    alike. This is a test of information leakage in the signal, not of the
    risk layer. With enforcement on, the buy-and-hold control trips the
    permanent drawdown_stop on shuffled paths while a vol-targeted book
    usually does not, so the "excess" measured the halt rather than any leak.

    Optional, all off by default so earlier results reproduce:
    `control` replaces the buy-and-hold control (a factory, like `factory`);
    `joint` shuffles every symbol with one shared permutation (see
    `shuffle_frame`); `run_kwargs` are passed to every `run` call, strategy
    and control, real and shuffled -- e.g. `start` and `rebalance_dates`.
    `enforce_risk` and `sources` are set here and may not appear in it.
    """
    extra = dict(run_kwargs or {})
    clash = {"enforce_risk", "sources"} & set(extra)
    if clash:
        raise ValueError(f"run_kwargs may not set {sorted(clash)}")
    make_control = control or (lambda _frame: BuyAndHold(symbols[0]))
    observed = metrics.sharpe(
        run(factory(frame), frame, symbols, sources=sources, enforce_risk=False, **extra)
        .equity.pct_change()
        .dropna()
    )

    shuffled: list[float] = []
    controls: list[float] = []
    for offset in range(n_runs):
        fake = shuffle_frame(frame, symbols, seed=seed + offset, joint=joint)
        result = run(factory(fake), fake, symbols, sources=sources, enforce_risk=False, **extra)
        shuffled.append(metrics.sharpe(result.equity.pct_change().dropna()))

        control_run = run(make_control(fake), fake, symbols, enforce_risk=False, **extra)
        controls.append(metrics.sharpe(control_run.equity.pct_change().dropna()))

    result = ShuffleTestResult(
        observed_sharpe=observed,
        shuffled_sharpes=shuffled,
        control_sharpes=controls,
        suspicious=False,
        threshold=threshold,
    )
    # Flag only when the excess is BOTH materially large and distinguishable
    # from zero. Either alone produces false alarms: a big mean from ten noisy
    # runs, or a tiny-but-consistent difference of no economic consequence.
    result.suspicious = result.mean_excess > threshold and result.t_stat > t_threshold
    return result


def determinism_check(
    factory: StrategyFactory,
    frame: pd.DataFrame,
    symbols: list[str],
    sources: Mapping[str, pd.DataFrame] | None = None,
    **run_options: Any,
) -> bool:
    """Same input must give byte-identical output. Hidden state fails here.

    `run_options` (fill, cost_bps, start, ...) are passed to both runs, so the
    check covers the configuration actually being validated."""
    first = run(factory(frame), frame, symbols, sources=sources, **run_options).equity
    second = run(factory(frame), frame, symbols, sources=sources, **run_options).equity
    return bool(first.equals(second))
