"""Point-in-time data access.

This is the single most important file in the repository.

Look-ahead bias -- using information that was not available at the moment a
decision was made -- is the bug that turns a losing strategy into a
spectacular backtest. It is silent, it is easy to introduce, and no linter or
type checker will ever find it.

So the defence is structural: strategies never touch a DataFrame. They
receive a PointInTimeView, which physically cannot return a row stamped later
than the decision time. An attempt to reach into the future raises.

EVENT-STAMPED SOURCES
---------------------
Daily bars are stamped with a DATE, and a bar dated t is invisible at as_of=t.
Some inputs are not daily bars: weekend crypto variance, for instance, becomes
known at an exact instant (Monday 13:00 UTC) that falls between the previous
close and the decision day's open. Stamping it with a date would either leak
it (dated the previous day, it would claim to be known at that day's close)
or lag it a full week (dated the decision day, it is invisible at that
decision).

So such data is passed as a named `source`: a frame indexed by the tz-aware
instant each row became known, read through `view.source(name)`, which is
itself a PointInTimeView truncated at the view's `instant` -- the tz-aware
moment the decision is executed. The harness sets `instant` to the equity
open of the decision day; the strict `<` rule then admits only rows known
before that open. `rewind()` narrows a view to an earlier decision and can
never widen it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

import pandas as pd


class LookAheadError(RuntimeError):
    """Raised when code attempts to read data from after the decision time."""


class PointInTimeView:
    """A read-only window onto market data, truncated at `as_of`.

    Parameters
    ----------
    frame:
        DataFrame with a sorted DatetimeIndex. Columns are symbols, or a
        MultiIndex of (symbol, field).
    as_of:
        The decision timestamp. Nothing at or after this instant is visible.
    sources:
        Optional named frames indexed by the tz-aware instant each row became
        known. Read only through `source(name)`.
    instant:
        Tz-aware execution instant bounding `sources`. Required if `sources`
        is given. For a tz-naive date `as_of` it must lie on that UTC day.

    Notes
    -----
    The boundary is deliberately strict: a bar stamped exactly `as_of` is
    NOT visible, because a daily bar timestamped 2026-01-05 is not complete
    until the close of 2026-01-05. Off-by-one here is the classic way to
    leak a day of future returns into a backtest.
    """

    __slots__ = ("_frame", "_as_of", "_sources", "_instant")

    def __init__(
        self,
        frame: pd.DataFrame,
        as_of: datetime | pd.Timestamp,
        sources: Mapping[str, pd.DataFrame] | None = None,
        instant: datetime | pd.Timestamp | None = None,
    ):
        if not isinstance(frame.index, pd.DatetimeIndex):
            raise TypeError("PointInTimeView requires a DatetimeIndex")
        if not frame.index.is_monotonic_increasing:
            raise ValueError("index must be sorted ascending")
        self._frame = frame
        self._as_of = pd.Timestamp(as_of)
        self._sources: dict[str, pd.DataFrame] = dict(sources or {})
        self._instant = _check_instant(self._as_of, instant, bool(self._sources))
        for name, source in self._sources.items():
            index = source.index
            if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
                raise TypeError(
                    f"source {name!r} must be indexed by the tz-aware instant each row became known"
                )
            if not index.is_monotonic_increasing:
                raise ValueError(f"source {name!r} index must be sorted ascending")

    @property
    def as_of(self) -> pd.Timestamp:
        return self._as_of

    @property
    def instant(self) -> pd.Timestamp | None:
        """Tz-aware execution instant bounding event-stamped sources, if any."""
        return self._instant

    def _visible(self) -> pd.DataFrame:
        return self._frame.loc[self._frame.index < self._as_of]

    def history(self, column: str, lookback: int | None = None) -> pd.Series:
        """Visible history for one column, oldest first."""
        series = self._visible()[column].dropna()
        if lookback is not None:
            series = series.iloc[-lookback:]
        return series

    def latest(self, column: str) -> float:
        """Most recent observed value. Raises if nothing is visible yet."""
        series = self.history(column)
        if series.empty:
            raise LookAheadError(f"no data for {column!r} available before {self._as_of}")
        return float(series.iloc[-1])

    def at(self, column: str, when: datetime | pd.Timestamp) -> float:
        """Value at an explicit timestamp. Raises on any future access."""
        stamp = pd.Timestamp(when)
        if stamp >= self._as_of:
            raise LookAheadError(
                f"attempted to read {column!r} at {stamp}, which is not "
                f"available at decision time {self._as_of}"
            )
        return float(self._frame.loc[stamp, column])

    def frame(self) -> pd.DataFrame:
        """A copy of everything visible. Copy, so callers cannot mutate."""
        return self._visible().copy()

    def __len__(self) -> int:
        return len(self._visible())

    def source(self, name: str) -> PointInTimeView:
        """An event-stamped source, truncated strictly before `instant`.

        Returns a PointInTimeView, so `history`, `latest` and `at` enforce the
        same boundary as they do for bars.
        """
        if name not in self._sources:
            raise KeyError(f"no source named {name!r}; available: {sorted(self._sources)}")
        return PointInTimeView(self._sources[name], as_of=self._instant)

    def rewind(
        self, as_of: datetime | pd.Timestamp, instant: datetime | pd.Timestamp | None = None
    ) -> PointInTimeView:
        """The same data seen from an EARLIER decision. Never later.

        Lets a strategy that holds a position for a week recompute exactly
        what it decided at the start of the week, from exactly what was
        visible then. Moving either boundary forward raises LookAheadError.
        """
        new_as_of = pd.Timestamp(as_of)
        if new_as_of > self._as_of:
            raise LookAheadError(f"cannot rewind forward from {self._as_of} to {new_as_of}")
        if self._sources:
            if instant is None:
                raise ValueError("this view has sources; rewind needs an explicit instant")
            new_instant = _check_instant(new_as_of, instant, required=True)
            if new_instant is None or self._instant is None or new_instant > self._instant:
                raise LookAheadError(
                    f"cannot rewind instant forward from {self._instant} to {new_instant}"
                )
        else:
            new_instant = None if instant is None else pd.Timestamp(instant)
        return PointInTimeView(
            self._frame, as_of=new_as_of, sources=self._sources, instant=new_instant
        )

    def __repr__(self) -> str:
        return f"PointInTimeView(as_of={self._as_of}, visible_rows={len(self)})"


def _check_instant(
    as_of: pd.Timestamp, instant: datetime | pd.Timestamp | None, required: bool
) -> pd.Timestamp | None:
    """Validate the execution instant against the bar boundary.

    For a tz-naive date `as_of` (daily bars), the instant must fall on that
    same UTC calendar day: a decision executed at t's open may not borrow an
    instant from a later day.
    """
    if instant is None:
        if required:
            raise ValueError("sources require a tz-aware decision instant")
        return None
    stamp = pd.Timestamp(instant)
    if stamp.tzinfo is None:
        raise ValueError("instant must be tz-aware")
    stamp = stamp.tz_convert("UTC")
    if as_of.tzinfo is None:
        naive = stamp.tz_localize(None)
        if not (as_of.normalize() <= naive < as_of.normalize() + pd.Timedelta(days=1)):
            raise LookAheadError(f"instant {stamp} is not on the decision day {as_of.date()}")
    elif stamp > as_of:
        raise LookAheadError(f"instant {stamp} is after as_of {as_of}")
    return stamp
