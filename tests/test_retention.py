from datetime import UTC, datetime

import pytest

from adv_assistant.db.retention import subtract_calendar_months


def test_subtract_calendar_months_handles_month_end_non_leap() -> None:
    value = datetime(2025, 3, 31, 12, 30, tzinfo=UTC)
    assert subtract_calendar_months(value, 1) == datetime(2025, 2, 28, 12, 30, tzinfo=UTC)


def test_subtract_calendar_months_handles_month_end_leap_year() -> None:
    value = datetime(2025, 3, 31, 8, 45, tzinfo=UTC)
    assert subtract_calendar_months(value, 13) == datetime(2024, 2, 29, 8, 45, tzinfo=UTC)


def test_subtract_calendar_months_keeps_value_for_zero() -> None:
    value = datetime(2026, 1, 15, 17, 0, tzinfo=UTC)
    assert subtract_calendar_months(value, 0) == value


def test_subtract_calendar_months_rejects_negative_months() -> None:
    value = datetime(2026, 1, 15, 17, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="months must be >= 0"):
        subtract_calendar_months(value, -1)
