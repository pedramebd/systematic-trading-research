"""Harness v2 research options: per-asset-class costs, market-on-close fills,
benchmark-relative deflated Sharpe, and the data manifest.

Every option is off by default and, off, a run must be exactly what it was.
The existing suites pin that path (and the PR's regression gate pins the
three validation scripts byte for byte). These cover the options themselves,
and above all that `fill="close"` cannot see the bar it fills on.
"""

from __future__ import annotations

import hashlib
import json
import sys

import numpy as np
import pandas as pd
import pytest

from tradingbot.config import Costs, load_limits
from tradingbot.data.pit import LookAheadError, PointInTimeView
from tradingbot.data.us_calendar import equity_open_utc
from tradingbot.research import manifest as manifest_module
from tradingbot.research import metrics
from tradingbot.research.backtest import build_frame, run
from tradingbot.research.costs import load_research_costs
from tradingbot.research.manifest import DataManifest, run_with_manifest
from tradingbot.strategies.base import Strategy
from tradingbot.strategies.buy_and_hold import BuyAndHold

LIMITS = load_limits()
ETF_BPS = LIMITS.costs.total_bps


def _frame(n: int = 260, seed: int = 7, symbols: tuple[str, ...] = ("SPY",)) -> pd.DataFrame:
    """Random walk whose open differs from the previous close and from its
    own close, so an open fill and a close fill are distinguishable."""
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2021-01-04", periods=n)
    bars = {}
    for symbol in symbols:
        close = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, n)))
        gap = np.exp(rng.normal(0.0, 0.004, n))
        open_ = np.r_[close[0], close[:-1]] * gap
        bars[symbol] = pd.DataFrame(
            {
                "open": open_,
                "high": np.maximum(open_, close) * 1.003,
                "low": np.minimum(open_, close) * 0.997,
                "close": close,
                "volume": 1e6,
            },
            index=index,
        )
    return build_frame(bars)


class Momentum(Strategy):
    """Long when the last visible close is above the one `lookback` bars
    earlier, flat otherwise. Price-dependent, so any leak would show."""

    name = "momentum_probe"

    def __init__(self, symbol: str = "SPY", lookback: int = 5):
        self.symbol = symbol
        self.lookback = lookback

    def warmup_periods(self) -> int:
        return self.lookback + 1

    def target_weights(self, view: PointInTimeView) -> dict[str, float]:
        closes = view.history(f"{self.symbol}_close", self.lookback + 1)
        return {self.symbol: 1.0 if closes.iloc[-1] > closes.iloc[0] else 0.0}


class Recorder(Momentum):
    """Momentum that also records what it could see, and tries to cheat."""

    name = "recorder"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen: list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp | None, bool]] = []

    def target_weights(self, view: PointInTimeView) -> dict[str, float]:
        try:
            view.at(f"{self.symbol}_close", view.as_of)
            peeked = True
        except LookAheadError:
            peeked = False
        last_visible = view.frame().index.max()
        self.seen.append((view.as_of, last_visible, view.instant, peeked))
        return super().target_weights(view)


# ===================================================================== fills


def test_fill_rejects_an_unknown_mode():
    with pytest.raises(ValueError, match="fill must be one of"):
        run(BuyAndHold("SPY"), _frame(), ["SPY"], fill="vwap")


def test_fill_open_is_the_default():
    frame = _frame()
    default = run(Momentum(), frame, ["SPY"])
    explicit = run(Momentum(), frame, ["SPY"], fill="open")
    pd.testing.assert_series_equal(default.equity, explicit.equity, check_exact=True)
    assert "fill" not in default.meta


def test_close_fill_sizes_at_the_open_and_fills_at_the_close():
    """A market-on-close order is a share quantity fixed before the close:
    sized off the open, filled at the close, marked at the same close."""
    frame = _frame()
    result = run(
        BuyAndHold("SPY"), frame, ["SPY"], fill="close", initial_equity=1000.0, enforce_risk=False
    )
    day1 = frame.index[1]
    shares = 1000.0 / frame.loc[day1, "SPY_open"]
    cost = shares * frame.loc[day1, "SPY_close"] * ETF_BPS / 1e4
    # Bought and marked at the same close: day one changes equity by the cost only.
    assert result.costs.iloc[0] == pytest.approx(cost, rel=1e-12)
    assert result.equity.iloc[0] == pytest.approx(1000.0 - cost, rel=1e-12)
    # Thereafter it is close-to-close on the shares bought.
    last = frame.index[-1]
    expected = (
        1000.0 - cost + shares * (frame.loc[last, "SPY_close"] - frame.loc[day1, "SPY_close"])
    )
    assert result.equity.iloc[-1] == pytest.approx(expected, rel=1e-12)
    assert result.meta["fill"] == "close"


def test_open_and_close_fills_differ_by_the_first_session_intraday_move():
    frame = _frame()
    day1 = frame.index[1]
    at_open = run(BuyAndHold("SPY"), frame, ["SPY"], initial_equity=1000.0, enforce_risk=False)
    at_close = run(
        BuyAndHold("SPY"), frame, ["SPY"], initial_equity=1000.0, enforce_risk=False, fill="close"
    )
    intraday = frame.loc[day1, "SPY_close"] / frame.loc[day1, "SPY_open"]
    gross_open = at_open.equity.iloc[0] + at_open.costs.iloc[0]
    assert gross_open == pytest.approx(1000.0 * intraday, rel=1e-12)
    assert at_close.equity.iloc[0] + at_close.costs.iloc[0] == pytest.approx(1000.0, rel=1e-12)


# ------------------------------------------------ close fill: no look-ahead


@pytest.mark.parametrize("fill", ["open", "close"])
def test_the_strategy_never_sees_the_bar_it_fills_on(fill):
    """Checked against the session the run actually trades, not against
    `view.as_of`: a harness that moved as_of forward would move both."""
    frame = _frame()
    strategy = Recorder()
    result = run(strategy, frame, ["SPY"], fill=fill, enforce_risk=False)
    assert len(strategy.seen) == len(result.weights)  # called once per session
    for session, (as_of, last_visible, _, peeked) in zip(
        result.weights.index, strategy.seen, strict=True
    ):
        assert as_of == session
        assert last_visible < session
        assert not peeked, f"view.at(as_of) succeeded at {session.date()}"


SESSIONS_TO_PROBE = range(10, 255, 9)


def _flip_from(frame: pd.DataFrame, t: pd.Timestamp, weight_at_t: float, keep_open: bool):
    """Rescale bar t and every later bar so that Momentum's signal AT t
    would flip if bar t were visible: up 10x if it was flat, down 10x if
    it was long. Returns the rewritten frame."""
    factor = 10.0 if weight_at_t == 0.0 else 0.1
    rewritten = frame.copy()
    later = rewritten.index >= t
    rewritten.loc[later] = rewritten.loc[later] * factor
    if keep_open:
        rewritten.loc[t, "SPY_open"] = frame.loc[t, "SPY_open"]
    # Self-check: a view that DID include bar t would decide differently, so
    # an unchanged weight below is evidence, not luck.
    leaky = PointInTimeView(rewritten, as_of=t + pd.Timedelta(hours=1))
    assert Momentum().target_weights(leaky)["SPY"] != weight_at_t
    return rewritten


def test_close_fill_decisions_ignore_the_fill_bar_and_everything_after():
    """Rewrite bar t entirely (open, high, low, close) and every later bar,
    in the direction that would flip a leaky decision. No weight at t or
    before may move. Risk off, so only the strategy decides."""
    frame = _frame()
    base = run(Momentum(), frame, ["SPY"], fill="close", enforce_risk=False)
    for pos in SESSIONS_TO_PROBE:
        t = frame.index[pos]
        rewritten = _flip_from(frame, t, base.weights.loc[t, "SPY"], keep_open=False)
        moved = run(Momentum(), rewritten, ["SPY"], fill="close", enforce_risk=False)
        upto = base.weights.index <= t
        pd.testing.assert_frame_equal(base.weights[upto], moved.weights[upto])
        before = base.equity.index < t
        pd.testing.assert_series_equal(base.equity[before], moved.equity[before])


def test_close_fill_with_risk_on_ignores_the_fill_bar_close():
    """Risk on: the risk check values positions at t's OPEN, known before a
    close fill, so t's open is kept. Its high, low and close, and every later
    bar, are rewritten. No weight at t or before may move."""
    frame = _frame()
    base = run(Momentum(), frame, ["SPY"], fill="close")
    for pos in SESSIONS_TO_PROBE:
        t = frame.index[pos]
        rewritten = _flip_from(frame, t, base.weights.loc[t, "SPY"], keep_open=True)
        moved = run(Momentum(), rewritten, ["SPY"], fill="close")
        upto = base.weights.index <= t
        pd.testing.assert_frame_equal(base.weights[upto], moved.weights[upto])


def test_close_fill_pnl_moves_with_the_fill_bar_close():
    """The other half: the close of t IS the fill price, so rewriting it
    must reach the equity on t when a position is held through it."""
    frame = _frame()
    base = run(Momentum(), frame, ["SPY"], fill="close", enforce_risk=False)
    t = base.weights.index[base.weights["SPY"].eq(1.0)][20]
    rewritten = frame.copy()
    rewritten.loc[t, "SPY_close"] *= 1.05
    moved = run(Momentum(), rewritten, ["SPY"], fill="close", enforce_risk=False)
    assert moved.equity[t] != base.equity[t]


def _rescale_bar_except_open(frame: pd.DataFrame, t: pd.Timestamp, factor: float):
    """Bar t's high, low and close (every symbol) and all later bars scaled
    by `factor`; bar t's opens kept, since they are known before a MOC order."""
    rewritten = frame.copy()
    rewritten.loc[rewritten.index >= t] *= factor
    opens = [c for c in frame.columns if c.endswith("_open")]
    rewritten.loc[t, opens] = frame.loc[t, opens]
    return rewritten


ORDER_CONFIGS = {
    "plain": {},
    "risk_off": {"enforce_risk": False},
    "monthly": "monthly",
    "start": "start",
    "cash": "cash",
}


@pytest.mark.parametrize("config", sorted(ORDER_CONFIGS))
@pytest.mark.parametrize("strategy_name", ["spy_gld_5050", "momentum"])
def test_close_fill_order_ignores_the_fill_bar_close(config, strategy_name):
    """Not just the target: WHETHER to trade (the rebalance band) and HOW MANY
    shares must not depend on bar t's close either. The fill is at that close,
    so cost_t / close_t recovers the shares traded; it must not move when the
    close (and everything after it) is rescaled, and a trade must not appear
    or vanish. Two symbols with the band in play, under every run option."""
    from tradingbot.strategies.spy_gld_5050 import SpyGld5050

    frame = _frame(symbols=("SPY", "GLD"))
    options: dict = {"fill": "close"}
    spec = ORDER_CONFIGS[config]
    if spec == "monthly":
        options["rebalance_dates"] = frame.index[frame.index.to_series().dt.month.diff().ne(0)]
    elif spec == "start":
        options["start"] = frame.index[40]
    elif spec == "cash":
        options["cash_accrual"] = pd.Series(0.0002, index=frame.index)
    else:
        options.update(spec)

    def make():
        return SpyGld5050() if strategy_name == "spy_gld_5050" else Momentum("SPY")

    base = run(make(), frame, ["SPY", "GLD"], **options)
    traded_somewhere = False
    traded_at = [frame.index.get_loc(d) for d in base.costs.index[base.costs.gt(0)]]
    probes = sorted(set(SESSIONS_TO_PROBE) | set(traded_at[1:12]))  # [0] is the entry
    for pos in probes:
        t = frame.index[pos]
        if t not in base.costs.index:
            continue
        factor = 1.0 + (0.03 if pos % 2 else -0.03)
        moved = run(make(), _rescale_bar_except_open(frame, t, factor), ["SPY", "GLD"], **options)
        assert (moved.costs[t] > 0) == (base.costs[t] > 0), f"trade appeared/vanished at {t}"
        # Same shares at a close `factor` higher: cost scales by exactly `factor`.
        assert moved.costs[t] == pytest.approx(base.costs[t] * factor, rel=1e-9, abs=1e-12)
        traded_somewhere |= bool(base.costs[t] > 0)
    assert traded_somewhere, "no probed session traded; the test proves nothing"


def test_close_fill_matches_a_run_on_data_truncated_at_the_fill_bar():
    """The run that stops at t had no future to look at. The full run must
    agree with it on every weight and every equity point through t."""
    frame = _frame()
    k = 180
    full = run(Momentum(), frame, ["SPY"], fill="close")
    cut = run(Momentum(), frame.iloc[: k + 1], ["SPY"], fill="close")
    pd.testing.assert_frame_equal(full.weights.iloc[: len(cut.weights)], cut.weights)
    pd.testing.assert_series_equal(full.equity.iloc[: len(cut.equity)], cut.equity)


def test_close_fill_keeps_sources_bounded_at_the_open(weekend_variance_sources, synthetic_frame):
    """Moving the fill to the close must not move the information set: a
    source row known after 09:30 on t is invisible to the decision at t."""
    strategy = Recorder()
    run(strategy, synthetic_frame, ["SPY"], sources=weekend_variance_sources, fill="close")
    for as_of, _, instant, _ in strategy.seen:
        assert instant == equity_open_utc(as_of)


# ===================================================================== costs


def test_cost_bps_equal_to_limits_reproduces_the_default():
    frame = _frame(symbols=("SPY", "GLD"))
    from tradingbot.strategies.spy_gld_5050 import SpyGld5050

    default = run(SpyGld5050(), frame, ["SPY", "GLD"])
    keyed = run(SpyGld5050(), frame, ["SPY", "GLD"], cost_bps={"SPY": ETF_BPS, "GLD": ETF_BPS})
    pd.testing.assert_series_equal(default.equity, keyed.equity, rtol=1e-12)
    pd.testing.assert_series_equal(default.costs, keyed.costs, rtol=1e-12)
    assert "cost_bps_by_symbol" not in default.meta


def test_cost_bps_charges_each_symbol_its_own_rate():
    frame = _frame(symbols=("SPY", "BTC"))

    class Half(Strategy):
        name = "half"

        def target_weights(self, view):
            return {"SPY": 0.5, "BTC": 0.5}

    result = run(
        Half(), frame, ["SPY", "BTC"], cost_bps={"SPY": 6.0, "BTC": 30.0}, initial_equity=1e6
    )
    # First session: 500k of each, bought at the open.
    assert result.costs.iloc[0] == pytest.approx(5e5 * 6e-4 + 5e5 * 30e-4, rel=1e-12)
    assert result.meta["cost_bps_by_symbol"] == {"SPY": 6.0, "BTC": 30.0}
    # limits.yaml's single rate was not charged, so it is not reported.
    assert "cost_bps_one_way" not in result.meta


def test_cost_bps_must_cover_every_symbol():
    frame = _frame(symbols=("SPY", "GLD"))
    with pytest.raises(ValueError, match="no entry for"):
        run(BuyAndHold("SPY"), frame, ["SPY", "GLD"], cost_bps={"SPY": 6.0})
    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite and not negative"):
            run(BuyAndHold("SPY"), frame, ["SPY", "GLD"], cost_bps={"SPY": 6.0, "GLD": bad})


def test_determinism_check_runs_the_configuration_given():
    from tradingbot.research import validation

    frame = _frame()
    calls = []

    def factory(_frame):
        calls.append(1)
        return Recorder()

    assert validation.determinism_check(
        factory, frame, ["SPY"], fill="close", cost_bps={"SPY": 30.0}
    )
    assert len(calls) == 2


def test_research_costs_file_etfs_inherit_limits_and_crypto_is_dearer():
    costs = load_research_costs()
    assert costs.bps_for("SPY") == ETF_BPS  # equity_etf -> limits.yaml
    assert costs.bps_for("GLD") == ETF_BPS  # commodity_etf -> limits.yaml
    for symbol in ("BTC-USD", "ETH-USD", "BTCUSDT", "BTC/USD"):
        assert costs.bps_for(symbol) >= 25.0  # at least Alpaca's taker fee
    assert costs.cost_bps(["SPY", "BTC-USD"]) == {
        "SPY": ETF_BPS,
        "BTC-USD": costs.bps_for("BTC-USD"),
    }


def test_research_costs_inherit_follows_limits(tmp_path):
    """`limits` means whatever limits.yaml says, not a copy of today's 6 bp."""
    from dataclasses import replace

    dearer = replace(LIMITS, costs=Costs(commission_bps=1.0, slippage_bps=9.0, spread_bps=4.0))
    costs = load_research_costs(limits=dearer)
    assert costs.bps_for("SPY") == 12.0


def test_research_costs_refuse_unknown_symbols_and_classes(tmp_path):
    costs = load_research_costs()
    with pytest.raises(KeyError, match="no asset class"):
        costs.bps_for("DOGE")
    with pytest.raises(KeyError, match="has no cost"):
        costs.bps_for("^VIX")  # an index: never traded, so never costed


def test_research_costs_refuse_a_class_conflict_with_the_universe(tmp_path):
    path = tmp_path / "costs.yaml"
    path.write_text("asset_classes: {crypto: limits}\nsymbols: {SPY: crypto}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="universe.yaml"):
        load_research_costs(path)


def test_research_costs_never_live_in_limits_yaml():
    """The research file must not become a second source of risk numbers."""
    from tradingbot.config import CONFIG_DIR

    assert "asset_classes" not in (CONFIG_DIR / "limits.yaml").read_text(encoding="utf-8")


# ================================================ relative deflated Sharpe


def _returns(seed: int, n: int = 750, mu: float = 0.0004) -> pd.Series:
    index = pd.bdate_range("2020-01-01", periods=n)
    return pd.Series(np.random.default_rng(seed).normal(mu, 0.01, n), index=index)


def test_relative_dsr_is_deflated_sharpe_of_the_difference():
    strategy, benchmark = _returns(1, mu=0.0008), _returns(2)
    active = strategy - benchmark
    for n_trials, trial_sharpes in ((1, None), (4, None), (4, [0.1, -0.3, 0.5, 0.2])):
        assert metrics.relative_deflated_sharpe(
            strategy, benchmark, n_trials, trial_sharpes=trial_sharpes
        ) == metrics.deflated_sharpe(active, n_trials, trial_sharpes=trial_sharpes)


def test_relative_dsr_is_not_the_absolute_dsr():
    """A strategy that IS the benchmark plus noise clears an absolute bar
    and fails the relative one."""
    benchmark = _returns(3, mu=0.001)
    strategy = benchmark + _returns(4, mu=0.0) * 0.1
    assert metrics.deflated_sharpe(strategy, 1) > 0.95
    assert metrics.relative_deflated_sharpe(strategy, benchmark, 1) < 0.95


def test_relative_dsr_refuses_misaligned_series():
    strategy, benchmark = _returns(1), _returns(2)
    with pytest.raises(ValueError, match="identical index"):
        metrics.relative_deflated_sharpe(strategy.iloc[1:], benchmark, 1)
    gapped = benchmark.copy()
    gapped.iloc[10] = np.nan
    with pytest.raises(ValueError, match="NaN"):
        metrics.relative_deflated_sharpe(strategy, gapped, 1)


# ================================================================= manifest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(manifest_module, "DATA_DIR", root)
    monkeypatch.setattr(manifest_module, "MANIFEST_DIR", root / "manifests")
    return root


def test_manifest_records_parquet_with_rows_dates_and_hash(data_dir):
    frame = pd.DataFrame({"x": [1.0, 2.0, 3.0]}, index=pd.bdate_range("2024-01-02", periods=3))
    path = data_dir / "bars.parquet"
    frame.to_parquet(path)
    manifest = DataManifest()
    original = pd.read_parquet
    with manifest.tracking():
        pd.read_parquet(path)
    assert pd.read_parquet is original, "read_parquet was not restored"
    (entry,) = manifest.entries()
    assert entry.rows == 3
    assert entry.first.startswith("2024-01-02") and entry.last.startswith("2024-01-04")
    assert entry.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert entry.sha256 in manifest.format()


def test_manifest_records_other_reads_under_data_but_not_writes_or_outside(data_dir, tmp_path):
    readme = data_dir / "provenance.json"
    readme.write_text("{}", encoding="utf-8")
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")
    manifest = DataManifest()
    with manifest.tracking():
        readme.read_text(encoding="utf-8")
        outside.read_text(encoding="utf-8")
        (data_dir / "written.txt").write_text("y", encoding="utf-8")
    names = [e.path.rsplit("/", 1)[-1] for e in manifest.entries()]
    assert names == ["provenance.json"]
    assert manifest.entries()[0].rows is None
    # Outside tracking, nothing is recorded.
    readme.read_text(encoding="utf-8")
    assert len(manifest.entries()) == 1


def test_run_with_manifest_off_is_just_main(data_dir, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(sys, "argv", ["validate_x.py", "--fast"])
    assert run_with_manifest(lambda: calls.append(1) or 0, "validate_x.py") == 0
    assert calls == [1]
    assert capsys.readouterr().out == ""
    assert not (data_dir / "manifests").exists()


def test_run_with_manifest_on_prints_and_writes(data_dir, monkeypatch, capsys):
    path = data_dir / "bars.parquet"
    pd.DataFrame({"x": [1.0]}, index=pd.bdate_range("2024-01-02", periods=1)).to_parquet(path)
    monkeypatch.setattr(sys, "argv", ["validate_x.py", "--manifest"])

    def main() -> int:
        print("result")
        pd.read_parquet(path)
        return 3

    assert run_with_manifest(main, "research/validate_x.py") == 3
    out = capsys.readouterr().out
    assert out.startswith("result\n")
    assert "DATA MANIFEST" in out and "bars.parquet" in out
    written = json.loads((data_dir / "manifests" / "validate_x.json").read_text(encoding="utf-8"))
    assert written["script"] == "validate_x"
    assert [f["rows"] for f in written["files"]] == [1]


def test_manifest_records_a_parquet_the_run_wrote_and_used_in_memory(data_dir):
    """The VIX case: fetched, cached to data/, used without being read back."""
    frame = pd.DataFrame({"x": [1.0, 2.0]}, index=pd.bdate_range("2024-01-02", periods=2))
    path = data_dir / "bars" / "idx_VIX.parquet"
    path.parent.mkdir()
    manifest = DataManifest()
    original = pd.DataFrame.to_parquet
    with manifest.tracking():
        frame.to_parquet(path)
    assert pd.DataFrame.to_parquet is original, "to_parquet was not restored"
    (entry,) = manifest.entries()
    assert (entry.access, entry.rows) == ("written", 2)
    assert entry.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_manifest_hashes_a_file_as_the_run_read_it(data_dir):
    path = data_dir / "provenance.json"
    path.write_text('{"v": 1}', encoding="utf-8")
    seen = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = DataManifest()
    with manifest.tracking():
        path.read_text(encoding="utf-8")
    path.write_text('{"v": 2}', encoding="utf-8")  # rewritten after the read
    assert manifest.entries()[0].sha256 == seen


def test_manifest_handles_a_partitioned_parquet_directory(data_dir):
    root = data_dir / "partitioned"
    root.mkdir()
    pd.DataFrame({"x": [1.0]}).to_parquet(root / "a.parquet")
    pd.DataFrame({"x": [2.0]}).to_parquet(root / "b.parquet")
    manifest = DataManifest()
    with manifest.tracking():
        pd.read_parquet(root)
    (entry,) = manifest.entries()
    assert entry.rows == 2 and len(entry.sha256) == 64
