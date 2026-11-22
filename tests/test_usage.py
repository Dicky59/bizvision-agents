from datetime import UTC, datetime

import pytest

from core.usage import month_bounds


def test_month_bounds_december_rolls_over_the_year() -> None:
    label, start, end = month_bounds("2026-12")
    assert label == "2026-12"
    assert start == datetime(2026, 12, 1, tzinfo=UTC) and end == datetime(2027, 1, 1, tzinfo=UTC)


def test_month_bounds_default_is_current_utc_month() -> None:
    label, start, end = month_bounds(None, datetime(2026, 11, 22, 23, 30, tzinfo=UTC))
    assert (label, start.day, end.month) == ("2026-11", 1, 12)


@pytest.mark.parametrize("bad", ["2026-13", "2026-00", "26-11", "2026-1", "november", ""])
def test_month_bounds_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        month_bounds(bad)
