"""Tests for the harness bugs the backtest-validator found.

Each test below fails against the old harness. They exist because the old
harness produced numbers that looked like strategy performance and were
actually artefacts:

  * a 10% drawdown halted the book permanently, so risk-enforced
    buy-and-hold was flat from February 2018 onwards and criteria 1, 2 and 6
    measured nothing;
  * a constant target weight still traded every single bar as prices moved,
    so buy-and-hold paid costs ~2900 times over eleven years.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd
import pytest

from tradingbot.config import load_limits
from tradingbot.research.backtest import build_frame, run
from tradingbot.strategies.base import Strategy
from tradingbot.strategies.buy_and_hold import BuyAndHold


def _frame(closes: list[float], start: str = "2020-01-01") -> pd.DataFrame:
    close = np.asarray(closes, dtype=float)
    idx = pd.date_range(start, periods=len(close), freq="B")
    bars = pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1e6},
        index=idx,
    )
    return build_frame({"SPY": bars})


@pytest.fixture
def drifting_frame() -> pd.DataFrame:
    """Steady upward drift, no drawdown deep enough to trip anything."""
    rng = np.random.default_rng(7)
    n = 500
    return _frame(list(100.0 * np.exp(np.cumsum(rng.normal(0.0006, 0.004, n)))))


@pytest.fixture
def dip_and_recover_frame() -> pd.DataFrame:
    """Falls ~13% -- past the 10% warn, short of the 15% stop -- then recovers."""
    flat = [100.0] * 30
    fall = list(np.linspace(100.0, 87.0, 25))
    rise = list(np.linspace(87.0, 112.0, 60))
    return _frame(flat + fall + rise)


# ---------- the no-trade band ----------


def test_buy_and_hold_trades_once_not_every_bar(drifting_frame):
    """Buy, then hold. One entry, nothing after."""
    result = run(BuyAndHold("SPY"), drifting_frame, ["SPY"], enforce_risk=False)
    trading_days = int((result.turnover > 1e-12).sum())
    assert trading_days == 1, (
        f"buy-and-hold traded on {trading_days} of {len(result.turnover)} bars; "
        "it should trade exactly once"
    )


class JitteryTarget(Strategy):
    """Target weight wobbles by +/-0.2% each bar, well inside the 0.5% band.

    This is what a vol-targeting strategy that refits daily actually looks
    like. Without a band, every one of those wobbles is a trade.
    """

    name = "jittery"

    def target_weights(self, view):
        return {"SPY": 0.60 + 0.002 * ((len(view) % 3) - 1)}


def test_band_suppresses_churn_from_a_jittery_target(drifting_frame):
    limits = load_limits()
    no_band = dataclasses.replace(
        limits, position=dataclasses.replace(limits.position, rebalance_band_pct=0.0)
    )

    banded = run(JitteryTarget(), drifting_frame, ["SPY"], enforce_risk=False)
    unbanded = run(JitteryTarget(), drifting_frame, ["SPY"], limits=no_band, enforce_risk=False)

    banded_days = int((banded.turnover > 1e-12).sum())
    unbanded_days = int((unbanded.turnover > 1e-12).sum())

    assert limits.position.rebalance_band_pct > 0
    assert unbanded_days > 400, (
        f"without a band only {unbanded_days} bars traded; the test strategy "
        "is not jittery enough to be a meaningful comparison"
    )
    # A band reduces churn, it does not eliminate it: with cash on the side
    # the held weight random-walks until it crosses the band, then snaps back.
    assert banded_days < unbanded_days / 5, (
        f"band let {banded_days} of {unbanded_days} trading days through"
    )

    # The entry trade costs the same either way and is unavoidable. What the
    # band changes is the churn after it.
    banded_churn = banded.total_costs() - float(banded.costs.iloc[0])
    unbanded_churn = unbanded.total_costs() - float(unbanded.costs.iloc[0])
    assert banded_churn < unbanded_churn / 5, (
        f"churn cost {banded_churn:.2f} banded vs {unbanded_churn:.2f} unbanded"
    )


def test_band_does_not_block_a_flatten_on_halt():
    """A halt must exit completely, band or no band."""
    crash = _frame([100.0] * 20 + list(np.linspace(100.0, 60.0, 60)))
    result = run(BuyAndHold("SPY"), crash, ["SPY"], enforce_risk=True)
    assert result.weights["SPY"].iloc[-1] == 0.0
    assert result.days_halted > 0


# ---------- drawdown_warn reduces, it does not halt ----------


def test_drawdown_warn_reduces_exposure_instead_of_halting(dip_and_recover_frame):
    result = run(BuyAndHold("SPY"), dip_and_recover_frame, ["SPY"], enforce_risk=True)
    weights = result.weights["SPY"]
    expected = load_limits().loss.drawdown_warn_exposure

    assert result.days_reduced > 0, "the warn level never engaged"
    assert np.isclose(weights.to_numpy(), expected).any(), (
        f"expected some bars at the reduced weight {expected}; saw {sorted(set(weights.round(3)))}"
    )
    assert not (weights == 0.0).all(), "warn must not flatten the book"
    assert result.days_halted == 0, "warn must not halt"


def test_drawdown_warn_lifts_itself_on_recovery(dip_and_recover_frame):
    result = run(BuyAndHold("SPY"), dip_and_recover_frame, ["SPY"], enforce_risk=True)
    weights = result.weights["SPY"]
    reduced = load_limits().loss.drawdown_warn_exposure

    at_reduced = np.isclose(weights.to_numpy(), reduced)
    assert at_reduced.any(), "never reduced"

    last_reduced = int(np.max(np.where(at_reduced)[0]))
    after = weights.to_numpy()[last_reduced + 1 :]
    assert len(after) > 0 and np.isclose(after, 1.0).all(), (
        "exposure did not return to full after the drawdown recovered"
    )


def test_drawdown_stop_is_still_permanent():
    """The warn level softened. The stop level must not have."""
    crash = _frame([100.0] * 20 + list(np.linspace(100.0, 60.0, 60)))
    result = run(BuyAndHold("SPY"), crash, ["SPY"], enforce_risk=True)
    assert (result.weights["SPY"].iloc[-15:] == 0.0).all()


def test_warn_exposure_may_only_reduce():
    """Constitution: the warn level can never maintain or raise exposure."""
    limits = load_limits()
    assert 0.0 <= limits.loss.drawdown_warn_exposure < 1.0


# ---------- shuffle test must not be contaminated by the risk layer ----------


def test_shuffle_test_runs_unenforced(monkeypatch, drifting_frame):
    """Regression for bug 7: the control ran risk-enforced, tripped the
    permanent drawdown_stop on shuffled paths, and the 'excess' measured the
    halt rather than any leak."""
    import tradingbot.research.validation as validation

    seen: list[bool] = []
    real_run = validation.run

    def spy(*args, **kwargs):
        seen.append(kwargs.get("enforce_risk", True))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(validation, "run", spy)
    validation.shuffle_test(lambda _: BuyAndHold("SPY"), drifting_frame, ["SPY"], n_runs=3)

    assert seen, "shuffle_test made no backtest calls"
    assert not any(seen), f"{sum(seen)} of {len(seen)} runs were risk-enforced"
