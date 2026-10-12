import numpy as np
import pandas as pd
import pytest

from tradingbot.research.backtest import build_frame


@pytest.fixture
def synthetic_frame() -> pd.DataFrame:
    """Deterministic geometric random walk. Fixed seed: tests must not flake."""
    rng = np.random.default_rng(12345)
    n = 750
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    returns = rng.normal(0.0004, 0.011, n)
    close = 100.0 * np.exp(np.cumsum(returns))
    bars = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.002,
            "low": close * 0.998,
            "close": close,
            "volume": 1_000_000.0,
        },
        index=idx,
    )
    return build_frame({"SPY": bars})


@pytest.fixture
def weekend_variance_sources(synthetic_frame) -> dict[str, pd.DataFrame]:
    """A synthetic event-stamped source: one row per week, known on Monday at
    13:00 UTC, before the US equity open. Stands in for the private repo's
    weekend-variance dataset; the harness only needs the contract (a frame
    indexed by the tz-aware instant each row became known)."""
    mondays = synthetic_frame.index[synthetic_frame.index.dayofweek == 0]
    known_at = (mondays + pd.Timedelta(hours=13)).tz_localize("UTC")
    rng = np.random.default_rng(7)
    values = rng.lognormal(mean=-8.0, sigma=0.5, size=len(known_at))
    frame = pd.DataFrame({"weekend_variance": values}, index=known_at)
    frame.index.name = "available_utc"
    return {"weekend_variance": frame}
