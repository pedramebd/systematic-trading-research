import pandas as pd
import pytest

from tradingbot.data.pit import LookAheadError, PointInTimeView


@pytest.fixture
def frame():
    idx = pd.date_range("2026-01-01", periods=10, freq="D")
    return pd.DataFrame({"close": range(10, 20)}, index=idx)


def test_bar_stamped_as_of_is_not_visible(frame):
    """A daily bar dated t is not complete until t closes."""
    view = PointInTimeView(frame, pd.Timestamp("2026-01-05"))
    assert len(view) == 4
    assert view.latest("close") == 13.0


def test_reading_the_future_raises(frame):
    view = PointInTimeView(frame, pd.Timestamp("2026-01-05"))
    with pytest.raises(LookAheadError):
        view.at("close", pd.Timestamp("2026-01-06"))


def test_reading_as_of_itself_raises(frame):
    view = PointInTimeView(frame, pd.Timestamp("2026-01-05"))
    with pytest.raises(LookAheadError):
        view.at("close", pd.Timestamp("2026-01-05"))


def test_empty_history_raises_rather_than_returning_nan(frame):
    view = PointInTimeView(frame, pd.Timestamp("2025-12-01"))
    with pytest.raises(LookAheadError):
        view.latest("close")


def test_frame_copy_cannot_mutate_source(frame):
    view = PointInTimeView(frame, pd.Timestamp("2026-01-05"))
    copy = view.frame()
    copy.loc[:, "close"] = 0
    assert frame["close"].iloc[0] == 10


def test_unsorted_index_rejected():
    idx = pd.to_datetime(["2026-01-03", "2026-01-01", "2026-01-02"])
    with pytest.raises(ValueError):
        PointInTimeView(pd.DataFrame({"close": [1, 2, 3]}, index=idx), "2026-01-04")


# --- event-stamped sources, instant and rewind ------------------------------


@pytest.fixture
def events():
    idx = pd.to_datetime(
        ["2026-01-05 13:00", "2026-01-05 14:30", "2026-01-05 15:00"], utc=True
    ).rename("known_at_utc")
    return pd.DataFrame({"value": [1.0, 2.0, 3.0]}, index=idx)


def test_source_is_truncated_strictly_before_instant(frame, events):
    view = PointInTimeView(
        frame, "2026-01-05", sources={"ev": events}, instant="2026-01-05 14:30+00:00"
    )
    source = view.source("ev")
    assert isinstance(source, PointInTimeView)
    assert list(source.frame()["value"]) == [1.0]  # 14:30 itself is not visible
    with pytest.raises(LookAheadError):
        source.at("value", pd.Timestamp("2026-01-05 14:30", tz="UTC"))


def test_sources_require_a_tz_aware_instant(frame, events):
    with pytest.raises(ValueError):
        PointInTimeView(frame, "2026-01-05", sources={"ev": events})
    with pytest.raises(ValueError):
        PointInTimeView(frame, "2026-01-05", sources={"ev": events}, instant="2026-01-05 14:30")


def test_instant_may_not_come_from_a_later_day(frame, events):
    with pytest.raises(LookAheadError):
        PointInTimeView(
            frame, "2026-01-05", sources={"ev": events}, instant="2026-01-06 00:00+00:00"
        )


def test_source_must_be_indexed_by_tz_aware_instants(frame, events):
    naive = events.copy()
    naive.index = naive.index.tz_localize(None)
    with pytest.raises(TypeError):
        PointInTimeView(frame, "2026-01-05", sources={"ev": naive}, instant="2026-01-05 14:30Z")


def test_unknown_source_raises(frame, events):
    view = PointInTimeView(frame, "2026-01-05", sources={"ev": events}, instant="2026-01-05 14:30Z")
    with pytest.raises(KeyError):
        view.source("nope")


def test_rewind_only_moves_backwards(frame, events):
    view = PointInTimeView(frame, "2026-01-07", sources={"ev": events}, instant="2026-01-07 14:30Z")
    earlier = view.rewind("2026-01-05", instant="2026-01-05 14:30Z")
    assert len(earlier) == 4
    assert list(earlier.source("ev").frame()["value"]) == [1.0]
    with pytest.raises(LookAheadError):
        view.rewind("2026-01-08", instant="2026-01-07 14:30Z")
    with pytest.raises(LookAheadError):
        # same day, later instant
        view.rewind("2026-01-07", instant="2026-01-07 15:00Z")
    with pytest.raises(ValueError):
        view.rewind("2026-01-05")  # sources present: instant is mandatory


def test_rewind_without_sources(frame):
    view = PointInTimeView(frame, "2026-01-08")
    assert view.rewind("2026-01-05").latest("close") == 13.0
    with pytest.raises(LookAheadError):
        view.rewind("2026-01-09")
