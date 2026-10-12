# `code/`: runnable extract

A self-contained subset of the private system: the point-in-time data view,
the research harness and the risk engine, with their tests. It runs on its
own. The tests use synthetic, seeded price paths, so no data download, API
key or network access is needed.

## How to run

```bash
cd code
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest                             # 145 tests, about 1-2 minutes
```

Python 3.10 or newer.

## What is here

```
CONSTITUTION.md                 the binding rules the code comments cite
config/limits.yaml              every risk number; the code enforces this file
config/universe.yaml            what may be traded vs only read as a signal
research/costs.yaml             per-asset-class research costs
src/tradingbot/
  config.py                     loads the YAML into frozen, validated dataclasses
  data/pit.py                   PointInTimeView: raises on any look-ahead
  data/us_calendar.py           NYSE calendar, DST-correct New York open in UTC
  research/backtest.py          event loop: decide on t-1, fill at t's open or close
  research/metrics.py           Sharpe, drawdown, (relative) deflated Sharpe
  research/validation.py        shuffle / leakage test, determinism check
  research/manifest.py          --manifest: hashes of every data file a run read
  research/costs.py             research cost model
  risk/engine.py                deterministic veto: loss, drawdown, exposure, universe
  risk/state.py                 halt registry; a FULL halt never lifts itself
  strategies/base.py            the Strategy contract: view in, target weights out
  strategies/buy_and_hold.py    the benchmark, and a zero-signal strategy
  strategies/trend_spy_gld.py   trial 3, as pre-registered
  strategies/spy_gld_5050.py    trial 3's control
  strategies/turn_of_month.py   trial 4, as pre-registered
tests/                          invariants, PIT, risk-limit breach attempts, harness
```

## A minimal example

```python
import numpy as np, pandas as pd
from tradingbot.research.backtest import build_frame, run
from tradingbot.strategies.buy_and_hold import BuyAndHold

idx = pd.date_range("2020-01-01", periods=750, freq="B")
close = 100 * np.exp(np.cumsum(np.random.default_rng(0).normal(4e-4, 0.011, 750)))
bars = pd.DataFrame({"open": close, "high": close, "low": close, "close": close,
                     "volume": 1e6}, index=idx)
result = run(BuyAndHold("SPY"), build_frame({"SPY": bars}), ["SPY"])
print(result.summary(), result.halt_events)
```

## Tests worth reading first

- `test_pit.py`: asking the view for the decision bar or anything later
  raises.
- `test_invariants.py`: zero signal produces exactly zero P&L, runs are
  reproducible, costs are charged, and a strategy that keeps its edge on
  shuffled returns is flagged as leaking.
- `test_risk_limits.py`: one test per rule, each trying to breach it.
- `test_risk_in_backtest.py` and `test_harness_fixes.py`: the backtest runs
  under the same risk engine, and halts fire on the bar they should. This
  includes regression tests for the daily- and weekly-loss bugs described
  in the top-level README.

## What is not here

Data ingestion, the broker adapter, the order runner, reconciliation, the
kill-switch CLI, monitoring, the briefing and the GitHub Actions workflows are
in the private repository. The top-level [README](../README.md) describes
them. The two crypto strategies (trials 1 and 2) are also left out, because
they depend on the private data pipeline.
