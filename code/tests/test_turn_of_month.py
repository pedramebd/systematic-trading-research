"""turn_of_month (pre-registration section 6) and its calendar helper."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradingbot.data.pit import PointInTimeView
from tradingbot.data.us_calendar import expected_closures, scheduled_sessions_to_month_end
from tradingbot.research.backtest import build_frame, run
from tradingbot.strategies.turn_of_month import TurnOfMonth


def _sessions(start: str, end: str) -> pd.DatetimeIndex:
    days = pd.bdate_range(start, end)
    return days.difference(expected_closures(start, end))


def _frame(index: pd.DatetimeIndex, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, len(index))))
    bars = pd.DataFrame(
        {"open": close, "high": close * 1.001, "low": close * 0.999, "close": close, "volume": 1e6},
        index=index,
    )
    return build_frame({"SPY": bars})


def _relative_positions(index: pd.DatetimeIndex) -> pd.DataFrame:
    """Independent re-derivation: each session's position from the start (+1,
    +2, ...) and from the end (-1, -2, ...) of its month, from the sessions."""
    month = pd.Series(index.year * 12 + index.month, index=index)
    from_start = month.groupby(month).cumcount() + 1
    from_end = -(month.groupby(month).cumcount(ascending=False) + 1)
    return pd.DataFrame({"start": from_start, "end": from_end})


@pytest.fixture
def frame() -> pd.DataFrame:
    return _frame(_sessions("2023-01-03", "2024-12-31"))


# ---- calendar helper ------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        ("2024-05-31", 1),  # Friday month-end
        ("2024-05-30", 2),
        ("2024-03-28", 1),  # Good Friday 2024-03-29: Thursday is the last session
        ("2024-03-27", 2),
        ("2021-12-31", 1),  # New Year on Saturday: NYSE trades the Friday
        ("2026-09-30", 1),
        ("2026-09-29", 2),
        ("2012-10-31", 1),  # Sandy closed the 29th and 30th; the SCHEDULE did not
        ("2012-10-26", 4),
        ("2012-10-25", 5),
    ],
)
def test_scheduled_sessions_to_month_end(day, expected):
    assert scheduled_sessions_to_month_end(pd.Timestamp(day)) == expected


def test_helper_ignores_one_off_closures():
    """2025-01-09 (Carter) is a SPECIAL_CLOSURE. The scheduled calendar counts
    it: it was not known in advance."""
    assert scheduled_sessions_to_month_end(pd.Timestamp("2025-01-08")) == 17
    actual = _sessions("2025-01-08", "2025-01-31")
    assert len(actual) == 16


def test_helper_matches_actual_sessions_when_no_one_off_closure(frame):
    positions = _relative_positions(frame.index)
    k = pd.Series([scheduled_sessions_to_month_end(d) for d in frame.index], index=frame.index)
    pd.testing.assert_series_equal(k, -positions["end"], check_names=False)


# ---- the rule -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fill", "held"),
    [("close", {-2, -1, 1, 2}), ("open", {-1, 1, 2, 3})],
)
def test_target_is_one_exactly_on_the_registered_sessions(frame, fill, held):
    strategy = TurnOfMonth(fill=fill)
    positions = _relative_positions(frame.index)
    for day in frame.index[1:]:
        want = positions.loc[day, "start"] in held or positions.loc[day, "end"] in held
        got = strategy.target_weights(PointInTimeView(frame, as_of=day))
        assert got == {"SPY": 1.0 if want else 0.0}, day.date()


def test_first_bar_without_history_is_cash(frame):
    view = PointInTimeView(frame, as_of=frame.index[0])
    assert TurnOfMonth().target_weights(view) == {"SPY": 0.0}


def test_decision_ignores_everything_from_the_decision_session_on(frame):
    """A leak of bar t (or later) would change the decision; nothing may."""
    strategy = TurnOfMonth()
    for day in frame.index[30:80]:
        tampered = frame.copy()
        tampered.loc[tampered.index >= day] *= 7.0
        truncated = frame.loc[frame.index <= day]
        base = strategy.target_weights(PointInTimeView(frame, as_of=day))
        assert strategy.target_weights(PointInTimeView(tampered, as_of=day)) == base
        assert strategy.target_weights(PointInTimeView(truncated, as_of=day)) == base


def test_sandy_month_uses_the_schedule():
    """Decision D8: the 26th is Day -4 by the schedule, so no purchase; the
    31st is Day -1, bought at its close, so October's Day -1 return is missed."""
    index = _sessions("2012-09-04", "2012-11-30").difference(
        pd.DatetimeIndex(["2012-10-29", "2012-10-30"])
    )
    data = _frame(index)
    strategy = TurnOfMonth()
    assert strategy.target_weights(PointInTimeView(data, as_of=pd.Timestamp("2012-10-26"))) == {
        "SPY": 0.0
    }
    assert strategy.target_weights(PointInTimeView(data, as_of=pd.Timestamp("2012-10-31"))) == {
        "SPY": 1.0
    }


def test_invalid_fill_is_rejected():
    with pytest.raises(ValueError):
        TurnOfMonth(fill="vwap")


# ---- through the harness --------------------------------------------------------


def test_close_fill_earns_exactly_days_minus_one_to_plus_three(frame):
    """Market-on-close, zero cost, no cash interest: the book's daily return is
    SPY's on Days -1, +1, +2, +3 and zero on every other session."""
    start = frame.index[25]  # 2023-02-08, mid-month: every later window is whole
    assert 5 <= _relative_positions(frame.index).loc[start, "start"] <= 10
    result = run(
        TurnOfMonth(),
        frame,
        ["SPY"],
        enforce_risk=False,
        start=start,
        fill="close",
        cost_bps={"SPY": 0.0},
    )
    book = result.equity.pct_change().dropna()
    spy = frame["SPY_close"].pct_change().reindex(book.index)
    positions = _relative_positions(frame.index).reindex(book.index)
    tom = positions["start"].isin([1, 2, 3]) | positions["end"].eq(-1)
    assert tom.sum() > 80
    np.testing.assert_allclose(book[tom], spy[tom], rtol=0, atol=1e-12)
    np.testing.assert_allclose(book[~tom], 0.0, rtol=0, atol=1e-12)


def test_strategy_is_deterministic(frame):
    kwargs = {"enforce_risk": False, "start": frame.index[25], "fill": "close"}
    first = run(TurnOfMonth(), frame, ["SPY"], **kwargs)
    second = run(TurnOfMonth(), frame, ["SPY"], **kwargs)
    assert first.equity.equals(second.equity)
