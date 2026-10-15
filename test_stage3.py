"""
Stage 3 tests — PATCH /attendance/{emp_code}/{date} (regularize).
Also adds threaded concurrency tests for punch-out (Stage 2 gap) and regularize.

Run:  .venv\\Scripts\\pytest test_stage1.py test_stage2.py test_stage3.py -v
"""
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app, db, utc_to_epoch_ms, IST

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ms(year, month, day, hour, minute, second=0):
    """Epoch ms for the given IST wall-clock time."""
    return utc_to_epoch_ms(
        datetime(year, month, day, hour, minute, second, tzinfo=IST)
    )


TEST_EMPS = ["EMP9901", "EMP9902"]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_db():
    db.employees.delete_many({"emp_code": {"$in": TEST_EMPS}})
    db.attendance_logs.delete_many({"emp_code": {"$in": TEST_EMPS}})
    yield
    db.employees.delete_many({"emp_code": {"$in": TEST_EMPS}})
    db.attendance_logs.delete_many({"emp_code": {"$in": TEST_EMPS}})


def make_employee(client, emp_code="EMP9901", shift_start="09:30", shift_end="18:30"):
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


def punch_in(client, emp_code="EMP9901", punch_ms=None, status="PRESENT"):
    if punch_ms is None:
        punch_ms = ms(2026, 7, 6, 9, 28)
    resp = client.post("/attendance/punch-in", json={
        "emp_code": emp_code, "punched_at": punch_ms, "status": status
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


def punch_out_req(client, emp_code="EMP9901", punch_ms=None):
    if punch_ms is None:
        punch_ms = ms(2026, 7, 6, 18, 30)
    return client.post("/attendance/punch-out", json={
        "emp_code": emp_code, "punched_at": punch_ms
    })


def regularize(client, emp_code="EMP9901", date="2026-07-06", **kwargs):
    """Send a PATCH request with required fields pre-filled."""
    body = {"reason": "test correction", "regularized_by": "hr.admin"}
    body.update(kwargs)
    return client.patch(f"/attendance/{emp_code}/{date}", json=body)


# ---------------------------------------------------------------------------
# Basic correction tests
# ---------------------------------------------------------------------------

class TestRegularizeBasic:

    def test_correct_punch_in_updates_late_minutes(self, client):
        """Correcting a late punch-in to before the grace period clears late_minutes."""
        make_employee(client)
        # Original: 10:05 IST → 35 min late
        punch_in(client, punch_ms=ms(2026, 7, 6, 10, 5))

        resp = regularize(client, punch_in=ms(2026, 7, 6, 9, 28))
        assert resp.status_code == 200
        data = resp.json()
        assert data["punch_in"]    == ms(2026, 7, 6, 9, 28)
        assert data["late_minutes"] == 0

    def test_history_appended_with_correct_fields(self, client):
        """History entry contains at, by, reason, and changes for the fields that changed."""
        make_employee(client)
        punch_in(client, punch_ms=ms(2026, 7, 6, 10, 5))

        resp = regularize(
            client,
            punch_in=ms(2026, 7, 6, 9, 28),
            reason="biometric glitch",
            regularized_by="hr.admin",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["history"]) == 1
        h = data["history"][0]
        assert h["by"]     == "hr.admin"
        assert h["reason"] == "biometric glitch"
        assert isinstance(h["at"], int)   # epoch ms
        # punch_in and late_minutes changed; nothing else should be in changes
        assert set(h["changes"].keys()) == {"punch_in", "late_minutes"}
        assert h["changes"]["punch_in"]["from"] == ms(2026, 7, 6, 10, 5)
        assert h["changes"]["punch_in"]["to"]   == ms(2026, 7, 6, 9, 28)
        assert h["changes"]["late_minutes"]["from"] == 35
        assert h["changes"]["late_minutes"]["to"]   == 0

    def test_second_correction_appends_second_history_entry(self, client):
        """Each correction appends; history is never overwritten."""
        make_employee(client)
        punch_in(client, punch_ms=ms(2026, 7, 6, 10, 5))

        regularize(client, punch_in=ms(2026, 7, 6, 9, 40))
        resp2 = regularize(client, punch_in=ms(2026, 7, 6, 9, 28))
        assert resp2.status_code == 200
        assert len(resp2.json()["history"]) == 2

    def test_correct_to_absent_clears_punch_times(self, client):
        """Setting status to ABSENT clears punch_in and punch_out."""
        make_employee(client)
        punch_in(client)
        punch_out_req(client)

        resp = regularize(client, status="ABSENT")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"]    == "ABSENT"
        assert data["punch_in"]  is None
        assert data["punch_out"] is None
        assert data["work_hours"]       is None
        assert data["late_minutes"]     == 0
        assert data["overtime_minutes"] == 0
        assert data["half_day"]         is False

    def test_correct_punch_out_recomputes_work_hours_ot_halfday(self, client):
        """Correcting punch_out recomputes work_hours, overtime, half_day."""
        make_employee(client)
        punch_in(client, punch_ms=ms(2026, 7, 6, 9, 30))
        punch_out_req(client, punch_ms=ms(2026, 7, 6, 13, 0))  # 3.5 h → half_day

        # Correct to a full day (19:10 → 40 min OT)
        resp = regularize(client, punch_out=ms(2026, 7, 6, 19, 10))
        assert resp.status_code == 200
        data = resp.json()
        assert data["work_hours"]       == 9.67   # 9h40m = 34800s/3600 = 9.666... → 9.67
        assert data["overtime_minutes"] == 40
        assert data["half_day"]         is False


# ---------------------------------------------------------------------------
# Validation tests (422)
# ---------------------------------------------------------------------------

class TestRegularizeValidation:

    def test_absent_with_punch_in_422(self, client):
        make_employee(client)
        punch_in(client)
        resp = regularize(client, status="ABSENT", punch_in=ms(2026, 7, 6, 9, 28))
        assert resp.status_code == 422

    def test_leave_with_punch_out_422(self, client):
        make_employee(client)
        punch_in(client)
        punch_out_req(client)
        resp = regularize(client, status="LEAVE", punch_out=ms(2026, 7, 6, 18, 30))
        assert resp.status_code == 422

    def test_presence_status_without_punch_in_422(self, client):
        """Setting PRESENT on a record that has no punch_in (was ABSENT) is 422."""
        make_employee(client)
        # Seed an ABSENT record directly
        db.attendance_logs.insert_one({
            "emp_code": "EMP9901", "date": "2026-07-06", "status": "ABSENT",
            "punch_in": None, "punch_out": None,
            "work_hours": None, "late_minutes": 0,
            "overtime_minutes": 0, "half_day": False, "history": [],
        })
        # Correct status to PRESENT but supply no punch_in → 422
        resp = regularize(client, status="PRESENT")
        assert resp.status_code == 422

    def test_no_change_422(self, client):
        make_employee(client)
        punch_in(client, punch_ms=ms(2026, 7, 6, 9, 28))
        # Send exactly what's already stored — nothing changes
        resp = regularize(client, punch_in=ms(2026, 7, 6, 9, 28))
        assert resp.status_code == 422

    def test_punch_in_wrong_date_422(self, client):
        """Corrected punch_in that resolves to a different IST date is 422 (R1)."""
        make_employee(client)
        punch_in(client, punch_ms=ms(2026, 7, 6, 9, 28))
        # Sending a punch_in for July 7 on a July 6 record
        resp = regularize(client, punch_in=ms(2026, 7, 7, 9, 28))
        assert resp.status_code == 422

    def test_punch_out_before_punch_in_422(self, client):
        make_employee(client)
        punch_in(client, punch_ms=ms(2026, 7, 6, 9, 30))
        resp = regularize(client, punch_out=ms(2026, 7, 6, 9, 30))  # equal → 422
        assert resp.status_code == 422

    def test_punch_out_more_than_24h_422(self, client):
        make_employee(client)
        pi = ms(2026, 7, 6, 9, 30)
        punch_in(client, punch_ms=pi)
        too_late = pi + 25 * 3600 * 1000
        resp = regularize(client, punch_out=too_late)
        assert resp.status_code == 422

    def test_record_not_found_404(self, client):
        make_employee(client)
        resp = regularize(client, date="2026-07-01")
        assert resp.status_code == 404

    def test_employee_not_found_404(self, client):
        resp = regularize(client, emp_code="EMP9999")
        assert resp.status_code == 404

    def test_failed_patch_leaves_record_and_history_unchanged(self, client):
        """A failing PATCH (422 or 409) must not mutate the record or its history."""
        make_employee(client)
        punch_in(client, punch_ms=ms(2026, 7, 6, 9, 28))  # late_minutes=0

        # 422: no-op — same punch_in already stored
        resp = regularize(client, punch_in=ms(2026, 7, 6, 9, 28))
        assert resp.status_code == 422

        # Record must be exactly as punched-in: no history, late_minutes=0
        record = db.attendance_logs.find_one({"emp_code": "EMP9901", "date": "2026-07-06"})
        assert len(record.get("history") or []) == 0
        assert record["late_minutes"] == 0

        # 422: ABSENT + punch_in is also a no-write
        resp2 = regularize(client, status="ABSENT", punch_in=ms(2026, 7, 6, 9, 28))
        assert resp2.status_code == 422
        record2 = db.attendance_logs.find_one({"emp_code": "EMP9901", "date": "2026-07-06"})
        assert len(record2.get("history") or []) == 0  # still no history

    def test_failed_patch_leaves_record_unchanged(self, client):
        """A 422 or 409 must not mutate the record or its history."""
        make_employee(client)
        punch_in(client, punch_ms=ms(2026, 7, 6, 9, 28))

        # 422: no-op correction (same punch_in)
        resp = regularize(client, punch_in=ms(2026, 7, 6, 9, 28))
        assert resp.status_code == 422

        # Record must be exactly as punched-in
        record = db.attendance_logs.find_one({"emp_code": "EMP9901", "date": "2026-07-06"})
        assert record["late_minutes"] == 0
        assert len(record.get("history") or []) == 0


# ---------------------------------------------------------------------------
# Concurrency tests (threading against real MongoDB)
# ---------------------------------------------------------------------------

class TestConcurrency:

    def test_concurrent_punch_out_one_200_one_409(self, client):
        """Two simultaneous punch-outs: exactly one 200, one 409."""
        make_employee(client)
        punch_in(client, punch_ms=ms(2026, 7, 6, 9, 30))
        po_ms = ms(2026, 7, 6, 18, 30)

        results = []

        def do_punch_out():
            # Each thread needs its own TestClient instance to avoid
            # shared state on the HTTPX transport.
            with TestClient(app) as c:
                r = c.post("/attendance/punch-out", json={
                    "emp_code": "EMP9901", "punched_at": po_ms
                })
                results.append(r.status_code)

        t1 = threading.Thread(target=do_punch_out)
        t2 = threading.Thread(target=do_punch_out)
        t1.start(); t2.start()
        t1.join();  t2.join()

        results.sort()
        assert results == [200, 409], f"expected [200, 409], got {results}"

    def test_concurrent_regularize_one_200_one_409_history_len_1(self, client):
        """Two simultaneous PATCHes: exactly one 200, one 409, history length = 1."""
        make_employee(client)
        punch_in(client, punch_ms=ms(2026, 7, 6, 10, 5))  # 35 min late

        results = []

        def do_regularize():
            with TestClient(app) as c:
                r = c.patch("/attendance/EMP9901/2026-07-06", json={
                    "punch_in":       ms(2026, 7, 6, 9, 28),
                    "reason":         "concurrent correction test",
                    "regularized_by": "hr.admin",
                })
                results.append(r.status_code)

        t1 = threading.Thread(target=do_regularize)
        t2 = threading.Thread(target=do_regularize)
        t1.start(); t2.start()
        t1.join();  t2.join()

        results.sort()
        assert results == [200, 409], f"expected [200, 409], got {results}"

        # Confirm the record has exactly one history entry — no silent overwrite
        record = db.attendance_logs.find_one({"emp_code": "EMP9901", "date": "2026-07-06"})
        assert len(record["history"]) == 1, (
            f"expected 1 history entry, got {len(record['history'])}"
        )
