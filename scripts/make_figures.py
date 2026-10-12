"""Equity curves of strategy vs control for every tested trial.

    python scripts/make_figures.py --source "/path/to/private/trading-bot"

This script is published so the method behind each figure is visible. It does
NOT run on its own: it imports the private research repository's frozen
validation scripts (`research/validate_*.py`) and reads their local, gitignored
research data snapshots (daily ETF bars, T-bill rates, weekend crypto variance).
No account, broker or paper-trading data is touched.

Each trial is re-run on the CURRENT harness (the corrected one), using that
trial's own loaders, strategy classes and settings. Before a figure is saved,
the Sharpe it reproduces is checked against the value the private registry
records, so a figure cannot silently drift from the published numbers.

Writes docs/figures/<trial>.png. Nothing else.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

OUT = Path(__file__).resolve().parents[1] / "docs" / "figures"

# Reference palette, light mode: categorical slots 1 and 2, muted ink for the
# information-only series, recessive grid.
STRATEGY = "#2a78d6"
CONTROL = "#eb6834"
INFO = "#8a8984"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
SURFACE = "#fcfcfb"

TOL = 5e-4  # Sharpe agreement with the recorded value


def load_module(source: Path, name: str):
    path = source / "research" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def check(label: str, got: float, expected: float) -> None:
    status = "ok" if abs(got - expected) <= TOL else "MISMATCH"
    print(f"  {label:<44} {got: .4f}  (recorded {expected: .4f})  {status}")
    if status != "ok":
        raise SystemExit(f"{label}: reproduced {got:.4f}, recorded {expected:.4f}")


def growth(returns: pd.Series) -> pd.Series:
    """Growth of 1 from a leading point of 1.0 the day before the first return."""
    curve = (1.0 + returns).cumprod()
    lead = pd.Series([1.0], index=[curve.index[0] - pd.Timedelta(days=1)])
    return pd.concat([lead, curve])


def drawdown(curve: pd.Series) -> pd.Series:
    return curve / curve.cummax() - 1.0


def plot(
    name: str, title: str, subtitle: str, series: list[tuple[str, pd.Series, str, str]]
):
    """series: (label, daily returns, color, linestyle). One y-axis per panel."""
    fig, (top, bottom) = plt.subplots(
        2,
        1,
        figsize=(9, 5.6),
        sharex=True,
        gridspec_kw={"height_ratios": [3, 1.3], "hspace": 0.08},
        facecolor=SURFACE,
    )
    for ax in (top, bottom):
        ax.set_facecolor(SURFACE)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.tick_params(colors=INK_2, labelsize=9, length=0)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(GRID)

    ends = []
    curves = []
    for label, returns, color, style in series:
        curve = growth(returns)
        width = 1.2 if color == INFO else 2.0
        z = 3 if color == STRATEGY else 2  # the strategy draws on top of the control
        top.plot(
            curve.index,
            curve.values,
            color=color,
            linewidth=width,
            linestyle=style,
            label=label,
            zorder=z,
        )
        ends.append((curve.index[-1], curve.iloc[-1]))
        curves.append(curve)
        dd = drawdown(curve)
        bottom.plot(
            dd.index,
            dd.values * 100,
            color=color,
            linewidth=width * 0.75,
            linestyle=style,
            zorder=z,
        )

    # End-value labels, nudged apart vertically when two would collide.
    low, high = top.get_ylim()
    min_gap = 0.04 * (high - low)
    placed: list[float] = []
    pad = (max(e[0] for e in ends) - min(c.index[0] for c in curves)) * 0.012
    for when, value in sorted(ends, key=lambda e: e[1], reverse=True):
        y = value
        for other in placed:
            if abs(y - other) < min_gap:
                y = other - min_gap
        placed.append(y)
        top.text(
            when + pad,
            y,
            f"{value:.2f}",
            va="center",
            fontsize=9,
            color=INK_2,
        )
    top.set_ylabel("growth of 1", color=INK_2, fontsize=9)
    bottom.set_ylabel("drawdown %", color=INK_2, fontsize=9)
    top.legend(frameon=False, fontsize=9, loc="upper left", labelcolor=INK)
    fig.suptitle(
        title, x=0.06, y=0.985, ha="left", fontsize=12.5, color=INK, weight="bold"
    )
    fig.text(0.06, 0.925, subtitle, ha="left", fontsize=9, color=INK_2)
    fig.subplots_adjust(left=0.08, right=0.93, top=0.89, bottom=0.07)
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / f"{name}.png", dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print(f"  wrote docs/figures/{name}.png")


# ---- trials 1 and 2: enforced runs, out-of-sample window ----------------------

OOS_START = pd.Timestamp(
    "2020-02-03"
)  # first week both forecasts exist (104-week burn-in)


def trial_1(source: Path) -> None:
    print("trial 1  crypto_weekend_vol")
    v = load_module(source, "validate_crypto_weekend_vol")
    bars = v.load_bars(v.SYMBOL)
    bars = bars.loc[bars.index >= v.START]
    frame = v.build_frame({v.SYMBOL: bars})
    sources = {v.WV_SOURCE: v.availability_indexed(v.load_weekend_variance())}

    def oos(strategy, enforce: bool) -> pd.Series:
        result = v.run(
            strategy, frame, [v.SYMBOL], enforce_risk=enforce, sources=sources
        )
        return v.slice_returns(v.daily_returns(result), OOS_START)

    crypto = oos(v.CryptoWeekendVol(v.SYMBOL), True)
    base = oos(v.HarRvVolTarget(v.SYMBOL), True)
    hold = oos(v.BuyAndHold(v.SYMBOL), False)
    check("OOS Sharpe, crypto (corrected harness)", v.metrics.sharpe(crypto), 0.7729)
    for label, returns in (("control", base), ("buy-and-hold, unenforced", hold)):
        print(
            f"  OOS Sharpe, {label:<33} {v.metrics.sharpe(returns): .4f}  (information)"
        )
    plot(
        "crypto_weekend_vol",
        "Trial 1  crypto_weekend_vol: FAIL",
        "SPY vol-targeting, out of sample 2020-02-03 .. 2026-09. Risk limits enforced; "
        "6 bp one-way costs.",
        [
            ("strategy: HAR-RV + weekend crypto variance", crypto, STRATEGY, "-"),
            ("control: identical HAR-RV, no crypto term", base, CONTROL, "-"),
            ("SPY buy-and-hold (information only)", hold, INFO, "--"),
        ],
    )


def trial_2(source: Path) -> None:
    print("trial 2  crypto_weekend_monday")
    v = load_module(source, "validate_crypto_weekend_monday")
    full = v.build_frame({v.SYMBOL: v.load_bars(v.SYMBOL), "VIX": v.load_bars("^VIX")})
    frame = full.loc[full.index >= v.START]
    sources = {v.WV_SOURCE: v.availability_indexed(v.load_weekend_variance())}

    def oos(strategy, enforce: bool) -> pd.Series:
        result = v.run(
            strategy, frame, [v.SYMBOL], enforce_risk=enforce, sources=sources
        )
        return v.slice_returns(v.daily_returns(result), OOS_START)

    crypto = oos(v.CryptoWeekendMonday(v.SYMBOL), True)
    base = oos(v.HarGkVixMonday(v.SYMBOL), True)
    hold = oos(v.BuyAndHold(v.SYMBOL), False)
    check("OOS Sharpe, crypto (corrected harness)", v.metrics.sharpe(crypto), 0.7500)
    for label, returns in (("control", base), ("buy-and-hold, unenforced", hold)):
        print(
            f"  OOS Sharpe, {label:<33} {v.metrics.sharpe(returns): .4f}  (information)"
        )
    plot(
        "crypto_weekend_monday",
        "Trial 2  crypto_weekend_monday: FAIL (primary p = 0.0506)",
        "Monday sizing from a HAR + VIX variance forecast, out of sample 2020-02-03 .. 2026-09. "
        "Risk limits enforced.",
        [
            ("strategy: HAR + VIX + weekend crypto variance", crypto, STRATEGY, "-"),
            ("control: identical model, no crypto term", base, CONTROL, "-"),
            ("SPY buy-and-hold (information only)", hold, INFO, "--"),
        ],
    )


# ---- trials 3 and 4: unenforced runs (the criteria basis), fixed window -------


def trial_3(source: Path) -> None:
    print("trial 3  trend_spy_gld")
    v = load_module(source, "validate_trend_spy_gld")
    bars = {s: pd.read_parquet(v.SNAPSHOT / f"{s}.parquet") for s in v.SYMBOLS}
    rate = pd.read_parquet(v.SNAPSHOT / "rate.parquet")["rate"]
    frame = v.build_frame(bars)
    frame = frame.loc[(frame.index >= v.DATA_START) & (frame.index <= v.EVAL_END)]
    start = frame.index[frame.index >= v.EVAL_START][0]
    accrual = v.accrual_series(rate, frame.index)
    kwargs = {
        "start": start,
        "rebalance_dates": v.first_sessions(frame.index),
        "cash_accrual": accrual,
    }

    def go(strategy):
        return v.run(strategy, frame, v.SYMBOLS, enforce_risk=False, **kwargs)

    runs = {
        "s": go(v.TrendSpyGld()),
        "c": go(v.SpyGld5050()),
        "b": go(v.BuyAndHold("SPY")),
    }
    check(
        "excess Sharpe, strategy (unenforced)",
        v.metrics.sharpe(v.excess_of(runs["s"], accrual)),
        0.6544,
    )
    check(
        "excess Sharpe, control (unenforced)",
        v.metrics.sharpe(v.excess_of(runs["c"], accrual)),
        0.7281,
    )
    ret = {k: v.returns_of(r).loc[lambda x: x.index >= start] for k, r in runs.items()}
    plot(
        "trend_spy_gld",
        "Trial 3  trend_spy_gld: FAIL",
        "10-month trend filter on SPY and GLD, 50% each, 2007-01 .. 2026-08. Cash earns the "
        "T-bill; no risk limits (criteria basis).",
        [
            ("strategy: trend filter, long or cash", ret["s"], STRATEGY, "-"),
            ("control: 50/50 SPY/GLD, always held", ret["c"], CONTROL, "-"),
            ("SPY buy-and-hold (information only)", ret["b"], INFO, "--"),
        ],
    )


def trial_4(source: Path) -> None:
    print("trial 4  turn_of_month")
    v = load_module(source, "validate_turn_of_month")
    spy = pd.read_parquet(v.SNAPSHOT / "SPY.parquet")
    rate = pd.read_parquet(v.SNAPSHOT / "rate.parquet")["rate"]
    full = v.build_frame({"SPY": spy})
    full = full.loc[full.index <= v.EVAL_END]
    frame = full.loc[full.index >= v.DATA_START]
    start = frame.index[frame.index >= v.EVAL_START][0]
    accrual = v.tbill.accrual_series(rate, frame.index)
    kwargs = {"start": start, "fill": "close", "cash_accrual": accrual}

    def go(strategy):
        return v.run(strategy, frame, v.SYMBOLS, enforce_risk=False, **kwargs)

    runs = {"s": go(v.TurnOfMonth()), "c": go(v.BuyAndHold("SPY"))}
    check(
        "excess Sharpe, strategy (unenforced)",
        v.metrics.sharpe(v.excess_of(runs["s"], accrual)),
        0.1525,
    )
    check(
        "excess Sharpe, control (unenforced)",
        v.metrics.sharpe(v.excess_of(runs["c"], accrual)),
        0.5853,
    )
    ret = {k: v.returns_of(r).loc[lambda x: x.index >= start] for k, r in runs.items()}
    plot(
        "turn_of_month",
        "Trial 4  turn_of_month: FAIL",
        "SPY over Day -1..+3 of each month, T-bills otherwise, 2008-05 .. 2026-09. "
        "Market-on-close fills; no risk limits (criteria basis).",
        [
            ("strategy: turn of the month only", ret["s"], STRATEGY, "-"),
            ("control: SPY buy-and-hold", ret["c"], CONTROL, "-"),
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, required=True, help="private repo checkout"
    )
    parser.add_argument("--only", nargs="*", type=int, choices=[1, 2, 3, 4])
    args = parser.parse_args()
    source = args.source.resolve()
    for number, fn in [(1, trial_1), (2, trial_2), (3, trial_3), (4, trial_4)]:
        if not args.only or number in args.only:
            fn(source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
