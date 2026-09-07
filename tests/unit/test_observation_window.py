"""观测时间范围不接受隐式时区或精度舍入。"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from opsswarm.observation_window import ObservationWindow

END = datetime(2026, 9, 7, tzinfo=UTC)


@pytest.mark.parametrize("invalid", ["naive", "equal", "reversed", "large", "fraction", "epoch"])
def test_invalid_window_is_rejected(invalid):
    start, end = END - timedelta(minutes=15), END
    if invalid == "naive":
        end = end.replace(tzinfo=None)
    elif invalid == "equal":
        start = end
    elif invalid == "reversed":
        start = end + timedelta(seconds=1)
    elif invalid == "large":
        start = end - timedelta(days=1, seconds=1)
    elif invalid == "fraction":
        end += timedelta(microseconds=1)
    else:
        start, end = datetime(1969, 1, 1, tzinfo=UTC), datetime(1969, 1, 2, tzinfo=UTC)
    with pytest.raises(ValidationError):
        ObservationWindow(window_start=start, window_end=end)


def test_utc_normalization_and_future_guard():
    local = END.astimezone(timezone(timedelta(hours=8)))
    window = ObservationWindow(window_start=local - timedelta(minutes=15), window_end=local)
    assert window.window_end == END and window.window_end.tzinfo is UTC
    # 整秒约束作用于 UTC，不能通过非整数秒时区偏移绕过。
    with pytest.raises(ValidationError):
        ObservationWindow(
            window_start=END - timedelta(minutes=15),
            window_end=END.replace(tzinfo=timezone(timedelta(microseconds=1))),
        )
    assert window.telemetry_minutes() == 15
    window.require_elapsed(END)
    with pytest.raises(ValueError, match="截止"):
        window.require_elapsed(END - timedelta(seconds=1))


@pytest.mark.parametrize("seconds", [59, 61, 3601, 7200])
def test_telemetry_does_not_silently_round_or_expand_window(seconds):
    window = ObservationWindow(window_start=END - timedelta(seconds=seconds), window_end=END)
    with pytest.raises(ValueError, match="完整分钟"):
        window.telemetry_minutes()
