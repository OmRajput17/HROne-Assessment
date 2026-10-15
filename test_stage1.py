"""Stage 1 unit tests — business-rule helpers only (no Mongo needed)."""
from datetime import datetime, timedelta, timezone

from app.main import (
    attendance_date_and_punch,
    compute_late_minutes,
    compute_overtime_minutes_full,
    compute_work_hours,
    half_up,
)

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# half_up rounding (R4 / R8)
# ---------------------------------------------------------------------------
def test_half_up_basic():
    # Python's round(1.005, 2) would give 1.0 (banker's) — half_up must give 1.01
    assert half_up(1.005, 2) == 1.01
    assert half_up(0.5, 0) == 1.0   # rounds up
    assert half_up(2.5, 0) == 3.0   # rounds up (banker's would give 2.0)
    assert half_up(9.1166666, 2) == 9.12
    assert half_up(9.0833333, 2) == 9.08


# ---------------------------------------------------------------------------
# R2 — late_minutes
# ---------------------------------------------------------------------------
def _punch(hour, minute, second=0, day=6):
    """Helper: build a UTC-aware datetime for a given IST time on 2026-07-{day}."""
    return datetime(2026, 7, day, hour, minute, second, tzinfo=IST).astimezone(timezone.utc)


def test_not_late_before_shift():
    # 09:28 IST, shift 09:30 → not late
    assert compute_late_minutes(_punch(9, 28), "09:30", "2026-07-06") == 0


def test_not_late_exactly_10_min():
    # 09:40:00 IST → exactly 600 s after 09:30 → NOT strictly more than 10 min
    assert compute_late_minutes(_punch(9, 40, 0), "09:30", "2026-07-06") == 0


def test_late_one_second_over_grace():
    # 09:40:01 IST → 601 s after 09:30 → late; late_minutes = floor(601/60) = 10
    assert compute_late_minutes(_punch(9, 40, 1), "09:30", "2026-07-06") == 10


def test_late_spec_example_45():
    # 10:15:59 IST → floor((10*60+15)*60+59 - 9*3600-30*60)/60 = floor(2759/60) = 45
    assert compute_late_minutes(_punch(10, 15, 59), "09:30", "2026-07-06") == 45


def test_late_emp0003_35():
    # EMP0003 sample: 10:05 IST, shift 09:30 → 35 min late
    assert compute_late_minutes(_punch(10, 5, 0), "09:30", "2026-07-06") == 35


# ---------------------------------------------------------------------------
# R3 — overtime_minutes
# ---------------------------------------------------------------------------
def test_overtime_emp0003_40():
    # 19:10 IST punch-out, shift_end 18:30 → 40 min → ≥ 30 → 40
    po = _punch(19, 10)
    assert compute_overtime_minutes_full(po, "18:30", "09:30", "2026-07-06") == 40


def test_overtime_under_threshold():
    # 18:45 IST → 15 min → < 30 → 0
    po = _punch(18, 45)
    assert compute_overtime_minutes_full(po, "18:30", "09:30", "2026-07-06") == 0


def test_overtime_exactly_30():
    # 19:00 IST → exactly 30 min → ≥ 30 → 30
    po = _punch(19, 0)
    assert compute_overtime_minutes_full(po, "18:30", "09:30", "2026-07-06") == 30


def test_overtime_overnight_emp0005():
    # EMP0005 shift 22:00–06:00, punch_out 06:40 IST on July 7
    # attendance_date = 2026-07-06 (shift started that day)
    # shift_end = 06:00 IST on July 7 → OT = 40 min
    po = datetime(2026, 7, 7, 6, 40, tzinfo=IST).astimezone(timezone.utc)
    assert compute_overtime_minutes_full(po, "06:00", "22:00", "2026-07-06") == 40


# ---------------------------------------------------------------------------
# R4 — work_hours
# ---------------------------------------------------------------------------
def test_work_hours_emp0001():
    # 09:28 → 18:35 IST = 9h 7m = 32820 s / 3600 = 9.1166... → half-up 2dp → 9.12
    pi = _punch(9, 28)
    po = _punch(18, 35)
    assert compute_work_hours(pi, po) == 9.12


def test_work_hours_emp0003():
    # 10:05 → 19:10 IST = 9h 5m = 32700 s / 3600 = 9.0833... → 9.08
    pi = _punch(10, 5)
    po = _punch(19, 10)
    assert compute_work_hours(pi, po) == 9.08


def test_work_hours_half_day():
    # 3.5 h exactly = 12600 s / 3600 = 3.5 → 3.5
    pi = datetime(2026, 7, 7, 9, 30, tzinfo=IST).astimezone(timezone.utc)
    po = datetime(2026, 7, 7, 13, 0, tzinfo=IST).astimezone(timezone.utc)
    assert compute_work_hours(pi, po) == 3.5


# ---------------------------------------------------------------------------
# R1 — attendance_date_and_punch (IST date + overnight logic)
# ---------------------------------------------------------------------------
def _ms(hour, minute, second=0, day=6, month=7, year=2026, ist_day=None):
    """Build epoch-ms for an IST time."""
    d = ist_day if ist_day is not None else day
    dt = datetime(year, month, d, hour, minute, second, tzinfo=IST)
    return int(dt.timestamp() * 1000)


def test_r1_normal_date():
    # 09:28 IST July 6 → attendance date 2026-07-06
    date_str, _ = attendance_date_and_punch(_ms(9, 28), "09:30", "18:30")
    assert date_str == "2026-07-06"


def test_r1_overnight_shift_start():
    # EMP0005: 21:55 IST July 6, shift 22:00–06:00 → IST time is NOT < 06:00 → date = 2026-07-06
    date_str, _ = attendance_date_and_punch(_ms(21, 55), "22:00", "06:00")
    assert date_str == "2026-07-06"


def test_r1_overnight_early_morning_belongs_to_previous_day():
    # 05:45 IST July 7, shift 22:00–06:00 → IST time < 06:00 → belongs to July 6
    date_str, _ = attendance_date_and_punch(_ms(5, 45, ist_day=7), "22:00", "06:00")
    assert date_str == "2026-07-06"


def test_r1_overnight_exactly_at_shift_end_is_NOT_previous():
    # 06:00:00 IST July 7, shift 22:00–06:00 → NOT strictly before 06:00 → date = 2026-07-07
    date_str, _ = attendance_date_and_punch(_ms(6, 0, ist_day=7), "22:00", "06:00")
    assert date_str == "2026-07-07"


def test_r1_truncation():
    # Sub-second ms: 09:40:00.900 IST → truncated to 09:40:00 UTC
    dt_ist = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    ms_with_sub = int(dt_ist.timestamp() * 1000) + 900  # add 900ms
    _, punch_utc = attendance_date_and_punch(ms_with_sub, "09:30", "18:30")
    assert punch_utc.microsecond == 0
    assert punch_utc.second == 0
    # The time should be 09:40:00 IST = 04:10:00 UTC
    assert punch_utc.hour == 4
    assert punch_utc.minute == 10


if __name__ == "__main__":
    import pytest, sys
    sys.exit(pytest.main([__file__, "-v"]))
