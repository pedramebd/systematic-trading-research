"""Performance statistics.

Includes the Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014), which is
the honest answer to "I tested twenty strategies and this was the best one".
The more candidates tested, the higher the bar the winner must clear.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd
from scipy.stats import norm

TRADING_DAYS = 252
EULER_MASCHERONI = 0.5772156649015329


def annualised_return(returns: pd.Series, periods: int = TRADING_DAYS) -> float:
    if returns.empty:
        return 0.0
    total = float((1.0 + returns).prod())
    years = len(returns) / periods
    if years <= 0 or total <= 0:
        return 0.0
    return total ** (1.0 / years) - 1.0


def annualised_vol(returns: pd.Series, periods: int = TRADING_DAYS) -> float:
    if len(returns) < 2:
        return 0.0
    return float(returns.std(ddof=1) * np.sqrt(periods))


def sharpe(returns: pd.Series, rf: float = 0.0, periods: int = TRADING_DAYS) -> float:
    """Annualised Sharpe. `rf` is an annual risk-free rate."""
    if len(returns) < 2:
        return 0.0
    excess = returns - rf / periods
    sd = excess.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(excess.mean() / sd * np.sqrt(periods))


def max_drawdown(equity: pd.Series) -> float:
    """Maximum peak-to-trough decline, as a positive fraction."""
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float(((peak - equity) / peak).max())


def deflated_sharpe(
    returns: pd.Series,
    n_trials: int,
    benchmark_sharpe: float = 0.0,
    periods: int = TRADING_DAYS,
    trial_sharpes: Sequence[float] | None = None,
) -> float:
    """Probability the true Sharpe exceeds the benchmark, after deflating for
    the number of strategies tested and for non-normal returns.

    Bailey & Lopez de Prado (2014). The expected maximum Sharpe under the null
    of no skill is

        SR* = sqrt(V) * [ (1 - g) * Z(1 - 1/N) + g * Z(1 - 1/(N e)) ]

    where ``V`` is the VARIANCE OF THE SHARPE RATIOS ACROSS THE N TRIALS and
    ``g`` is the Euler-Mascheroni constant.

    ``trial_sharpes`` supplies those N annualised Sharpe ratios -- they live in
    ``research/registry.yaml``, one per candidate ever tested. Pass them.

    Without them the function falls back to ``sqrt(V) = 1/sqrt(n-1)``, the
    sampling standard error of a single Sharpe estimate under the null. That
    is a common approximation but it is NOT the cross-trial dispersion, and it
    understates the deflation whenever the candidates tested were more varied
    than chance alone would make them. A result reported without
    ``trial_sharpes`` should say so.

    Returns a probability in [0, 1]. Below 0.95 means the result is not
    distinguishable from luck given how many things were tried.
    """
    n = len(returns)
    if n < 3 or n_trials < 1:
        return 0.0

    sr = sharpe(returns, periods=periods) / np.sqrt(periods)  # per-period
    skew = float(returns.skew())
    kurt = float(returns.kurtosis()) + 3.0  # pandas gives excess kurtosis

    if n_trials > 1:
        if trial_sharpes is not None and len(trial_sharpes) >= 2:
            per_period = np.asarray(trial_sharpes, dtype=float) / np.sqrt(periods)
            sr_sigma = float(np.std(per_period, ddof=1))
        else:
            sr_sigma = 1.0 / np.sqrt(n - 1)
        e_max = (1 - EULER_MASCHERONI) * norm.ppf(1 - 1.0 / n_trials) + EULER_MASCHERONI * norm.ppf(
            1 - 1.0 / (n_trials * np.e)
        )
        sr0 = sr_sigma * e_max
    else:
        sr0 = benchmark_sharpe / np.sqrt(periods)

    denom = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr**2
    if denom <= 0:
        return 0.0

    z = (sr - sr0) * np.sqrt(n - 1) / np.sqrt(denom)
    return float(norm.cdf(z))


def active_returns(returns: pd.Series, benchmark_returns: pd.Series) -> pd.Series:
    """Strategy minus benchmark, day by day, on an identical index.

    Refuses mismatched or gapped series rather than aligning them: a silent
    inner join can drop exactly the days that matter.
    """
    if not returns.index.equals(benchmark_returns.index):
        raise ValueError(
            "strategy and benchmark returns must share an identical index "
            f"({len(returns)} vs {len(benchmark_returns)} periods)"
        )
    active = (returns - benchmark_returns).rename("active")
    if active.isna().any():
        raise ValueError(f"{int(active.isna().sum())} NaN active return(s)")
    return active


def relative_deflated_sharpe(
    returns: pd.Series,
    benchmark_returns: pd.Series,
    n_trials: int,
    periods: int = TRADING_DAYS,
    trial_sharpes: Sequence[float] | None = None,
) -> float:
    """`deflated_sharpe` on the strategy-minus-benchmark daily return series.

    Answers "does it beat the benchmark beyond luck?" rather than "does it
    beat cash?" -- which any equity-heavy strategy clears trivially. The null
    is an active Sharpe of zero (`benchmark_sharpe=0`), deflated for
    `n_trials` exactly as in `deflated_sharpe`.

    ``trial_sharpes``, if given, must be ACTIVE Sharpes too (each trial
    against its own pre-declared benchmark). Absolute Sharpes would set the
    deflation from the wrong distribution.
    """
    return deflated_sharpe(
        active_returns(returns, benchmark_returns),
        n_trials=n_trials,
        benchmark_sharpe=0.0,
        periods=periods,
        trial_sharpes=trial_sharpes,
    )


def summary(equity: pd.Series, n_trials: int = 1) -> dict[str, float]:
    returns = equity.pct_change().dropna()
    return {
        "total_return": float(equity.iloc[-1] / equity.iloc[0] - 1.0) if len(equity) else 0.0,
        "cagr": annualised_return(returns),
        "annual_vol": annualised_vol(returns),
        "sharpe": sharpe(returns),
        "max_drawdown": max_drawdown(equity),
        "deflated_sharpe_prob": deflated_sharpe(returns, n_trials=n_trials),
        "n_periods": float(len(returns)),
    }
