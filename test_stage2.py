"""
Stage 2 tests — punch-in and punch-out business rules.

Unit tests (no Mongo): boundary cases for every rule.
Integration tests: use FastAPI TestClient with a real MongoDB connection.

Run:  .venv\Scripts\pytest test_stage1.py test_stage2.py -v
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import (
    attendance_date_and_punch,
    compute_late_minutes,
    compute_overtime_minutes_full,
    compute_work_hours,
    half_up,
    app,
    db,
    utc_to_epoch_ms,
    epoch_ms_to_utc,
    IST,
)

# ---------------------------------------------------------------------------
# Helpers shared by tests
# ---------------------------------------------------------------------------

def ist(year, month, day, hour, minute, second=0):
    """Return a tz-aware UTC datetime for the given IST wall-clock time."""
    return datetime(year, month, day, hour, minute, second, tzinfo=IST).astimezone(timezone.utc)


def ms(year, month, day, hour, minute, second=0):
    """Epoch ms for the given IST wall-clock time."""
    return utc_to_epoch_ms(ist(year, month, day, hour, minute, second))


# ---------------------------------------------------------------------------
# Unit tests — R2 boundary cases (late_minutes)
# ---------------------------------------------------------------------------

class TestLateMinutes:
    """R2: late only if > 600 seconds after shift_start."""

    def test_exactly_600s_not_late(self):
        # 09:40:00 IST = exactly 600 s after 09:30 → NOT strictly > 600 → 0
        assert compute_late_minutes(ist(2026, 7, 6, 9, 40, 0), "09:30", "2026-07-06") == 0

    def test_601s_is_late_returns_10(self):
        # 09:40:01 IST = 601 s → late; floor(601/60) = 10
        assert compute_late_minutes(ist(2026, 7, 6, 9, 40, 1), "09:30", "2026-07-06") == 10

    def test_10min_59s_is_late_returns_10(self):
        # 09:40:59 IST = 659 s → late; floor(659/60) = 10 (not 11)
        assert compute_late_minutes(ist(2026, 7, 6, 9, 40, 59), "09:30", "2026-07-06") == 10

    def test_45_minutes_late(self):
        # spec example: 10:15:59 IST → floor(2759/60) = 45
        assert compute_late_minutes(ist(2026, 7, 6, 10, 15, 59), "09:30", "2026-07-06") == 45

    def test_before_shift_start_not_late(self):
        assert compute_late_minutes(ist(2026, 7, 6, 9, 15, 0), "09:30", "2026-07-06") == 0


# ---------------------------------------------------------------------------
# Unit tests — R3 boundary cases (overtime_minutes)
# ---------------------------------------------------------------------------

class TestOvertimeMinutes:
    """R3: overtime only when >= 30 minutes after shift_end."""

    def test_29_minutes_is_zero(self):
        # 18:59 IST (29 min after 18:30) → 0
        assert compute_overtime_minutes_full(
            ist(2026, 7, 6, 18, 59), "18:30", "09:30", "2026-07-06"
        ) == 0

    def test_exactly_30_minutes(self):
        # 19:00 IST (30 min after 18:30) → 30
        assert compute_overtime_minutes_full(
            ist(2026, 7, 6, 19, 0), "18:30", "09:30", "2026-07-06"
        ) == 30

    def test_40_minutes(self):
        assert compute_overtime_minutes_full(
            ist(2026, 7, 6, 19, 10), "18:30", "09:30", "2026-07-06"
        ) == 40

    def test_overnight_40_minutes(self):
        # EMP0005: shift 22:00–06:00, punch_out 06:40 IST July 7
        # attendance_date = 2026-07-06 (shift started that day)
        # shift_end = 06:00 IST on July 7 → OT = 40 min
        punch_out = ist(2026, 7, 7, 6, 40)
        assert compute_overtime_minutes_full(
            punch_out, "06:00", "22:00", "2026-07-06"
        ) == 40


# ---------------------------------------------------------------------------
# Unit tests — R4 / R5 boundary cases (work_hours, half_day)
# ---------------------------------------------------------------------------

class TestWorkHours:
    """R4: work_hours rounded half-up 2 dp. R5: half_day when < 4.50."""

    def test_4_495_rounds_to_4_50_not_half_day(self):
        # 4.495 hours = 16182 seconds; half-up → 4.50 → NOT half_day
        seconds = 16182  # 4*3600 + 0*60 + 4*60 + ... let's compute directly
        # 4.495 h * 3600 = 16182 s exactly
        pi = ist(2026, 7, 6, 9, 30)
        po = pi + timedelta(seconds=16182)
        wh = compute_work_hours(pi, po)
        assert wh == 4.50
        assert wh >= 4.50  # NOT half_day (R5: strictly < 4.50)

    def test_4_49_is_half_day(self):
        # 4h 29min 24s = 16164 s → 16164/3600 = 4.49 → half_day
        pi = ist(2026, 7, 6, 9, 30)
        po = pi + timedelta(seconds=16164)
        wh = compute_work_hours(pi, po)
        assert wh == 4.49
        assert wh < 4.50

    def test_exactly_4_50_is_not_half_day(self):
        # 4h 30m = 16200 s → 4.50 → NOT half_day
        pi = ist(2026, 7, 6, 9, 30)
        po = pi + timedelta(seconds=16200)
        wh = compute_work_hours(pi, po)
        assert wh == 4.50
        assert not (wh < 4.50)

    def test_work_hours_half_up_rounding(self):
        # 9h 7m = 32820 s → 32820/3600 = 9.11666... → half-up 2dp → 9.12
        pi = ist(2026, 7, 6, 9, 28)
        po = ist(2026, 7, 6, 18, 35)
        assert compute_work_hours(pi, po) == 9.12


# ---------------------------------------------------------------------------
# Integration tests — punch-in and punch-out endpoints
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    """TestClient wraps the FastAPI app. Lifespan runs, so indexes are created."""
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_db():
    """Wipe test employees and attendance logs before each test."""
    db.employees.delete_many({"emp_code": {"$in": ["EMP9901", "EMP9902"]}})
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP9901", "EMP9902"]}})
    yield
    db.employees.delete_many({"emp_code": {"$in": ["EMP9901", "EMP9902"]}})
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP9901", "EMP9902"]}})


def make_employee(client, emp_code="EMP9901", shift_start="09:30", shift_end="18:30"):
    """Helper: create a test employee."""
    resp = client.post("/employees", json={
        "emp_code":    emp_code,
        "name":        "Test User",
        "email":       f"test{emp_code.lower()}@example.com",
        "department":  "Testing",
        "shift_start": shift_start,
        "shift_end":   shift_end,
        "joined_on":   "2026-01-01",
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestPunchIn:

    def test_punch_in_creates_record(self, client):
        make_employee(client)
        punched_at = ms(2026, 7, 6, 9, 28)
        resp = client.post("/attendance/punch-in", json={
            "emp_code":   "EMP9901",
            "punched_at": punched_at,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["emp_code"]  == "EMP9901"
        assert data["date"]      == "2026-07-06"
        assert data["status"]    == "PRESENT"
        assert data["punch_in"]  == punched_at
        assert data["punch_out"] is None
        assert data["work_hours"] is None
        assert data["late_minutes"]     == 0
        assert data["overtime_minutes"] == 0
        assert data["half_day"]         is False
        assert data["history"]          == []

    def test_punch_in_late_35_minutes(self, client):
        make_employee(client)
        # 10:05 IST → 35 min late
        resp = client.post("/attendance/punch-in", json={
            "emp_code":   "EMP9901",
            "punched_at": ms(2026, 7, 6, 10, 5),
        })
        assert resp.status_code == 201
        assert resp.json()["late_minutes"] == 35

    def test_punch_in_exactly_10min_grace_not_late(self, client):
        make_employee(client)
        # 09:40:00 IST = exactly 600s → NOT late
        resp = client.post("/attendance/punch-in", json={
            "emp_code":   "EMP9901",
            "punched_at": ms(2026, 7, 6, 9, 40, 0),
        })
        assert resp.status_code == 201
        assert resp.json()["late_minutes"] == 0

    def test_punch_in_one_second_over_grace(self, client):
        make_employee(client)
        # 09:40:01 IST = 601s → late; late_minutes = 10
        resp = client.post("/attendance/punch-in", json={
            "emp_code":   "EMP9901",
            "punched_at": ms(2026, 7, 6, 9, 40, 1),
        })
        assert resp.status_code == 201
        assert resp.json()["late_minutes"] == 10

    def test_punch_in_unknown_employee_404(self, client):
        resp = client.post("/attendance/punch-in", json={
            "emp_code":   "EMP9999",
            "punched_at": ms(2026, 7, 6, 9, 28),
        })
        assert resp.status_code == 404

    def test_duplicate_punch_in_409(self, client):
        make_employee(client)
        payload = {"emp_code": "EMP9901", "punched_at": ms(2026, 7, 6, 9, 28)}
        r1 = client.post("/attendance/punch-in", json=payload)
        r2 = client.post("/attendance/punch-in", json=payload)
        assert r1.status_code == 201
        assert r2.status_code == 409

    def test_punch_in_seconds_value_rejected_422(self, client):
        # A value that looks like Unix seconds (below 100_000_000_000)
        resp = client.post("/attendance/punch-in", json={
            "emp_code":   "EMP9901",
            "punched_at": 1783312500,   # seconds, not ms → rejected
        })
        assert resp.status_code == 422

    def test_punch_in_wfh_status(self, client):
        make_employee(client)
        resp = client.post("/attendance/punch-in", json={
            "emp_code":   "EMP9901",
            "punched_at": ms(2026, 7, 6, 9, 28),
            "status":     "WFH",
        })
        assert resp.status_code == 201
        assert resp.json()["status"] == "WFH"

    def test_punch_in_absent_status_rejected_422(self, client):
        # ABSENT is not allowed at punch-in (only PRESENT/WFH/ON_DUTY)
        make_employee(client)
        resp = client.post("/attendance/punch-in", json={
            "emp_code":   "EMP9901",
            "punched_at": ms(2026, 7, 6, 9, 28),
            "status":     "ABSENT",
        })
        assert resp.status_code == 422

    def test_punch_in_overnight_shift_correct_date(self, client):
        # EMP0005-style: shift 22:00–06:00. Punch at 05:45 IST July 7 →
        # that's before shift_end (06:00) → attendance date = July 6
        make_employee(client, "EMP9902", shift_start="22:00", shift_end="06:00")
        resp = client.post("/attendance/punch-in", json={
            "emp_code":   "EMP9902",
            "punched_at": ms(2026, 7, 7, 5, 45),
        })
        assert resp.status_code == 201
        assert resp.json()["date"] == "2026-07-06"


class TestPunchOut:

    def _punch_in(self, client, punch_ms):
        make_employee(client)
        resp = client.post("/attendance/punch-in", json={
            "emp_code":   "EMP9901",
            "punched_at": punch_ms,
        })
        assert resp.status_code == 201
        return resp.json()

    def test_punch_out_computes_work_hours(self, client):
        pi = ms(2026, 7, 6, 9, 28)
        self._punch_in(client, pi)
        po = ms(2026, 7, 6, 18, 35)
        resp = client.post("/attendance/punch-out", json={
            "emp_code":   "EMP9901",
            "punched_at": po,
        })
        assert resp.status_code == 200
        data = resp.json()
        # 09:28–18:35 IST = 9h 7m = 32820 s / 3600 = 9.1166... → 9.12
        assert data["work_hours"]       == 9.12
        assert data["overtime_minutes"] == 0   # 5 min OT < 30 min threshold
        assert data["half_day"]         is False
        assert data["punch_out"]        == po

    def test_punch_out_overtime_40(self, client):
        # 10:05 → 19:10 IST: OT = 40 min (>= 30)
        pi = ms(2026, 7, 6, 10, 5)
        self._punch_in(client, pi)
        resp = client.post("/attendance/punch-out", json={
            "emp_code":   "EMP9901",
            "punched_at": ms(2026, 7, 6, 19, 10),
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["overtime_minutes"] == 40
        assert data["work_hours"]       == 9.08

    def test_punch_out_overtime_29_is_zero(self, client):
        # punch out 18:59 IST (29 min after 18:30) → OT = 0
        pi = ms(2026, 7, 6, 9, 30)
        self._punch_in(client, pi)
        resp = client.post("/attendance/punch-out", json={
            "emp_code":   "EMP9901",
            "punched_at": ms(2026, 7, 6, 18, 59),
        })
        assert resp.status_code == 200
        assert resp.json()["overtime_minutes"] == 0

    def test_punch_out_exactly_30min_ot(self, client):
        # punch out at exactly 19:00 IST → OT = 30
        pi = ms(2026, 7, 6, 9, 30)
        self._punch_in(client, pi)
        resp = client.post("/attendance/punch-out", json={
            "emp_code":   "EMP9901",
            "punched_at": ms(2026, 7, 6, 19, 0),
        })
        assert resp.status_code == 200
        assert resp.json()["overtime_minutes"] == 30

    def test_punch_out_half_day_true(self, client):
        # 09:30 → 13:00 IST = 3.5 h < 4.50 → half_day=True
        pi = ms(2026, 7, 6, 9, 30)
        self._punch_in(client, pi)
        resp = client.post("/attendance/punch-out", json={
            "emp_code":   "EMP9901",
            "punched_at": ms(2026, 7, 6, 13, 0),
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["work_hours"] == 3.5
        assert data["half_day"]   is True

    def test_punch_out_exactly_4_50_not_half_day(self, client):
        # 09:30 → 14:00 IST = 4.5 h → NOT half_day (R5: strictly < 4.50)
        pi = ms(2026, 7, 6, 9, 30)
        self._punch_in(client, pi)
        resp = client.post("/attendance/punch-out", json={
            "emp_code":   "EMP9901",
            "punched_at": ms(2026, 7, 6, 14, 0),
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["work_hours"] == 4.5
        assert data["half_day"]   is False

    def test_punch_out_before_punch_in_422(self, client):
        pi = ms(2026, 7, 6, 9, 30)
        self._punch_in(client, pi)
        # punch_out == punch_in → 422 (must be AFTER)
        resp = client.post("/attendance/punch-out", json={
            "emp_code":   "EMP9901",
            "punched_at": pi,
        })
        assert resp.status_code == 422

    def test_punch_out_more_than_24h_422(self, client):
        pi = ms(2026, 7, 6, 9, 30)
        self._punch_in(client, pi)
        # 25 hours later → 422
        too_late = pi + 25 * 3600 * 1000
        resp = client.post("/attendance/punch-out", json={
            "emp_code":   "EMP9901",
            "punched_at": too_late,
        })
        assert resp.status_code == 422

    def test_punch_out_unknown_employee_404(self, client):
        resp = client.post("/attendance/punch-out", json={
            "emp_code":   "EMP9999",
            "punched_at": ms(2026, 7, 6, 18, 30),
        })
        assert resp.status_code == 404

    def test_punch_out_no_open_record_404(self, client):
        make_employee(client)
        # No punch-in at all
        resp = client.post("/attendance/punch-out", json={
            "emp_code":   "EMP9901",
            "punched_at": ms(2026, 7, 6, 18, 30),
        })
        assert resp.status_code == 404

    def test_duplicate_punch_out_409(self, client):
        pi = ms(2026, 7, 6, 9, 30)
        self._punch_in(client, pi)
        po_payload = {"emp_code": "EMP9901", "punched_at": ms(2026, 7, 6, 18, 30)}
        r1 = client.post("/attendance/punch-out", json=po_payload)
        r2 = client.post("/attendance/punch-out", json=po_payload)
        assert r1.status_code == 200
        assert r2.status_code == 409

    def test_punch_out_overnight_shift(self, client):
        # EMP0005-style: shift 22:00–06:00.
        # Punch in 21:55 IST July 6 (normal start), punch out 06:40 IST July 7.
        # OT = 40 min (06:40 vs 06:00). attendance_date = 2026-07-06.
        make_employee(client, "EMP9902", shift_start="22:00", shift_end="06:00")
        pi = ms(2026, 7, 6, 21, 55)
        resp = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9902", "punched_at": pi
        })
        assert resp.status_code == 201
        assert resp.json()["date"] == "2026-07-06"

        resp = client.post("/attendance/punch-out", json={
            "emp_code":   "EMP9902",
            "punched_at": ms(2026, 7, 7, 6, 40),
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["overtime_minutes"] == 40
        assert data["work_hours"]       == 8.75


class TestListAttendance:

    def test_list_returns_records(self, client):
        make_employee(client)
        client.post("/attendance/punch-in", json={
            "emp_code": "EMP9901", "punched_at": ms(2026, 7, 6, 9, 28)
        })
        resp = client.get("/attendance", params={"emp_code": "EMP9901"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert data["items"][0]["emp_code"] == "EMP9901"

    def test_list_date_from_after_date_to_422(self, client):
        resp = client.get("/attendance", params={
            "date_from": "2026-07-10", "date_to": "2026-07-01"
        })
        assert resp.status_code == 422
