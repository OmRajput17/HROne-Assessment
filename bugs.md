# Starter `app/main.py` — Bug List (for REVIEW.md)

This file lists every defect found in the original starter, with line references.
Use this as raw material for REVIEW.md — write the final text in your own words.

---

## Bugs Found

### Bug 1 — `compute_late_minutes` uses wrong timezone (line ~22)
**Function:** `compute_late_minutes`
**Line:** ~22 (`start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)`)
**What is wrong:** `punch_in` is a naive UTC datetime (or local-time, see Bug 11), and `shift_start` is an IST time. The function builds a "shift start" datetime by replacing the hour/minute fields on the `punch_in` object directly — but those fields are in UTC, not IST. So comparing a UTC time against an IST-based shift-start is meaningless; e.g. shift_start "09:30 IST" is 04:00 UTC, but the code would build a datetime at 09:30 UTC.
**How you'd notice it:** Any employee whose IST punch-in time correctly crosses the grace-period boundary would return a wrong `late_minutes` value. For EMP0003 (shift 09:30 IST, punches in at 10:05 IST = 04:35 UTC), the code builds start=04:05 UTC instead of 04:00 UTC, giving 30 min instead of the correct 35 min.
**Fix:** Convert `punch_in` to IST first, then build the shift-start on the IST date with the correct IST offset.

---

### Bug 2 — `compute_late_minutes` grace-period logic is off (line ~24)
**Function:** `compute_late_minutes`
**Line:** ~24 (`minutes = int((punch_in - start).total_seconds() / 60)` then `return minutes if minutes > 10 else 0`)
**What is wrong:** R2 says "strictly more than 10:00 after shift_start". `int(total_seconds / 60)` truncates sub-minute seconds before the comparison. A punch-in 10 minutes and 59 seconds late gives `int(659/60) = 10`, then `10 > 10` is False → returns 0. But 10:59 is strictly more than 10:00, so it should return 10. (The returned value also uses `minutes` which is already the floor since `shift_start` — that part is correct per R2. The bug is only in the grace-period threshold check.)
**How you'd notice it:** `punch_in` at exactly shift_start + 10:01 through shift_start + 10:59 would incorrectly return 0 instead of 10.
**Fix:** Compare raw `total_seconds > 600` (600 seconds = 10 minutes exactly), then compute `late_minutes = int(total_seconds / 60)` for the return value.

---

### Bug 3 — `compute_work_hours` uses banker's rounding (line ~28)
**Function:** `compute_work_hours`
**Line:** ~28 (`return round((punch_out - punch_in).total_seconds() / 3600, 2)`)
**What is wrong:** Python's built-in `round()` uses banker's rounding (round-half-to-even). R4 requires half-up rounding. E.g. a session of exactly 1.5 hours + 30 seconds / 3600 = some value whose second decimal is exactly .5 would be rounded the wrong direction.
**How you'd notice it:** Any duration whose seconds/3600 value has exactly .005 at the third decimal would be rounded down instead of up.
**Fix:** Use `decimal.Decimal` with `ROUND_HALF_UP`, working in integer seconds: `Decimal(seconds) / Decimal(3600)`, then round to 2 decimal places half-up.

---

### Bug 4 — `compute_overtime` mixes tz-aware and naive datetimes (line ~32–34)
**Function:** `compute_overtime`
**Lines:** ~32–34
```python
end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
return max(0, int((punch_out - end).total_seconds() / 60))
```
**What is wrong:** `punch_out` is a naive datetime (local time or UTC), but `end` is tz-aware (IST). Python raises `TypeError: can't subtract offset-naive and offset-aware datetimes`. If it somehow runs (e.g. both happen to be naive), the comparison is still in the wrong timezone.
**How you'd notice it:** Any call to the punch-out endpoint would raise a 500 error.
**Fix:** Keep all stored datetimes as tz-aware UTC. Build `shift_end` in UTC by subtracting the IST offset.

---

### Bug 5 — `compute_overtime` doesn't apply the 30-minute minimum threshold (line ~34)
**Function:** `compute_overtime`
**Line:** ~34 (`return max(0, int((punch_out - end).total_seconds() / 60))`)
**What is wrong:** R3 says overtime is counted only when `punch_out - shift_end >= 30 minutes`. The code uses `max(0, ...)` which gives any positive overtime, even 1 minute.
**How you'd notice it:** An employee who leaves 15 minutes after shift end would be credited with 15 overtime minutes, but the rule says 0.
**Fix:** `ot = int(...)` then `return ot if ot >= 30 else 0`.

---

### Bug 6 — `GET /health` never pings MongoDB (line ~51)
**Function:** `health`
**Line:** ~51
**What is wrong:** The endpoint always returns `{"status": "ok"}` regardless of whether MongoDB is reachable. The spec says "Returns 200 only if MongoDB answers a ping, otherwise 503."
**How you'd notice it:** If MongoDB is down, the grader polls `/health` and gets 200 → proceeds to test endpoints → every endpoint crashes. The grader interprets this as a broken API rather than a down database.
**Fix:** Run `db.command("ping")` in a try/except; return 503 on any exception.

---

### Bug 7 — `POST /employees` has a check-then-insert race condition (line ~56)
**Function:** `create_employee`
**Lines:** ~56–57
```python
if db.employees.find_one({"emp_code": body.emp_code}):
    raise HTTPException(409, "emp_code already exists")
```
**What is wrong:** Two concurrent requests both pass the `find_one` check before either inserts, then both insert → two documents with the same `emp_code`.
**How you'd notice it:** A load test with two simultaneous POST requests for the same `emp_code` creates duplicates instead of one 201 and one 409.
**Fix:** Remove the `find_one` check entirely. Rely on a unique index on `emp_code`, catch `DuplicateKeyError` from `insert_one`, and raise 409.

---

### Bug 8 — `POST /employees` stores a naive local-time `created_at` (line ~60)
**Function:** `create_employee`
**Line:** ~60 (`doc["created_at"] = datetime.now()`)
**What is wrong:** `datetime.now()` returns a naive datetime in the server's local timezone, not UTC. Should be `datetime.now(timezone.utc)` (tz-aware UTC).
**How you'd notice it:** `created_at` values are inconsistent between machines with different system clocks/timezones.
**Fix:** `datetime.now(timezone.utc)`.

---

### Bug 9 — `POST /employees` response returns `created_at` as a Python datetime, not epoch ms (line ~62–63)
**Function:** `create_employee`
**Line:** ~62–63 (returns `doc` directly)
**What is wrong:** The API contract says every instant in a response is epoch milliseconds (integer). Returning a raw Python datetime object will either serialize to an ISO string (FastAPI's default) or raise a serialisation error — not an integer.
**How you'd notice it:** The `created_at` field in the response is an ISO string like `"2026-07-06T04:30:00"` instead of e.g. `1783312500000`.
**Fix:** Convert datetimes to `int(dt.timestamp() * 1000)` before returning.

---

### Bug 10 — `GET /employees` pagination uses wrong skip formula (line ~69)
**Function:** `list_employees`
**Line:** ~69 (`skip = page * page_size`)
**What is wrong:** Page 1 should skip 0 records, but this formula gives `1 * page_size = 20`. The correct formula is `(page - 1) * page_size`.
**How you'd notice it:** Page 1 returns records 21–40 instead of 1–20.
**Fix:** `skip = (page - 1) * page_size`.

---

### Bug 11 — `GET /employees` `total` ignores the `department` filter (line ~70)
**Function:** `list_employees`
**Line:** ~70 (`total = db.employees.count_documents({})`)
**What is wrong:** The count always returns the total number of employees across all departments, even when a `department` filter is applied. R10 says "`total` always reflects the applied filters."
**How you'd notice it:** Filtering by `department=Engineering` (2 employees) returns `total: 6` instead of `total: 2`.
**Fix:** `total = db.employees.count_documents(q)`.

---

### Bug 12 — `POST /attendance/punch-in` doesn't check for employee existence (line ~76)
**Function:** `punch_in`
**Line:** ~76–77 (`emp = db.employees.find_one(...)` then immediately uses `emp["shift_start"]` without a None check)
**What is wrong:** If the employee doesn't exist, `find_one` returns `None`, and `emp["shift_start"]` raises `KeyError` → unhandled 500. The spec requires a 404.
**How you'd notice it:** `POST /attendance/punch-in` with a non-existent `emp_code` crashes with 500 instead of returning 404.
**Fix:** Check `if not emp: raise HTTPException(404, "employee not found")`.

---

### Bug 13 — `POST /attendance/punch-in` computes attendance date in UTC, not IST (line ~78)
**Function:** `punch_in`
**Line:** ~78 (`d = ts.date().isoformat()`)
**What is wrong:** `ts` is built without timezone info (see Bug 14), so `.date()` gives the UTC date. For IST (UTC+5:30), the attendance date should be the IST calendar date. An employee punching in at 23:30 UTC on July 6 is actually at 05:00 IST on July 7 — wrong date is recorded.
**Also:** Overnight-shift logic is completely absent.
**How you'd notice it:** Night-shift workers (or anyone punching in between 00:00–05:29 UTC) get the wrong attendance date.
**Fix:** Convert `ts` to IST, handle overnight shift logic (R1), then take `.date()`.

---

### Bug 14 — `POST /attendance/punch-in` creates a naive local-time datetime from epoch ms (line ~77)
**Function:** `punch_in`
**Line:** ~77 (`ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()`)
**What is wrong:** `datetime.fromtimestamp(...)` without a `tz` argument returns a naive datetime in the server's local time. Should be `datetime.fromtimestamp(..., tz=timezone.utc)` to get a tz-aware UTC datetime.
**How you'd notice it:** Results are different on machines with different system timezones.
**Fix:** `datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)`.

---

### Bug 15 — `POST /attendance/punch-in` check-then-insert race condition (line ~79)
**Function:** `punch_in`
**Line:** ~79 (`if db.attendance_logs.find_one(...)`)
**What is wrong:** Same as Bug 7. Two simultaneous punch-ins for the same (emp_code, date) can both pass the check and both insert, creating duplicates.
**How you'd notice it:** Concurrent test for the same employee on the same day creates two records and both return 201 instead of one 201 and one 409.
**Fix:** Rely on a unique compound index on `(emp_code, date)`, catch `DuplicateKeyError`.

---

### Bug 16 — `POST /attendance/punch-in` response has wrong shape (lines ~90–91)
**Function:** `punch_in`
**Lines:** ~90–91
```python
doc["id"] = str(res.inserted_id)
doc.pop("_id", None)
```
**What is wrong:** (a) The API contract has no `id` field — attendance records are addressed by `(emp_code, date)`, never by `_id`. (b) `punch_in` in `doc` is a Python datetime, not epoch milliseconds. The contract requires epoch ms.
**How you'd notice it:** The response contains an extra `"id"` field and datetimes serialise as strings, not integers.
**Fix:** Remove the `id` line; convert all datetime fields to epoch ms before returning.

---

### Bug 17 — `GET /attendance` loads entire collection into memory (lines ~102–107)
**Function:** `list_attendance`
**Lines:** ~102–107
```python
docs = list(db.attendance_logs.find(q))
docs.sort(key=lambda d: d["date"], reverse=True)
total = len(docs)
page_docs = docs[(page - 1) * page_size : page * page_size]
```
**What is wrong:** The entire collection is loaded into Python memory, sorted in Python, and then sliced. On 100,000 records this will be extremely slow and memory-intensive, and the grader explicitly notes "must stay fast on a collection of ~100,000 records; do not load the collection into memory."
**How you'd notice it:** The endpoint hangs or OOMs under load.
**Fix:** Use MongoDB's `.sort()` + `.skip()` + `.limit()` on the cursor, with a supporting index on `(date, emp_code)`. Use `count_documents(q)` for the total.

---

### Bug 18 — `GET /attendance` returns `_id` as `"id"` and raw datetimes (line ~108)
**Function:** `list_attendance`
**Line:** ~108 (`d["id"] = str(d.pop("_id"))`)
**What is wrong:** (a) The contract has no `id` field. (b) Datetime fields are Python datetime objects serialised as strings, not epoch milliseconds.
**Fix:** Exclude `_id` with `{"_id": 0}` in the projection; convert datetimes to epoch ms.

---

### Bug 19 — No indexes are created at startup
**What is wrong:** No indexes beyond `_id` exist. Every query does a full collection scan (COLLSCAN). The grader checks that `/admin/explain/{endpoint}` shows IXSCAN.
**Fix:** Create these indexes at startup (idempotently):
- `employees`: unique index on `emp_code`
- `attendance_logs`: unique compound index on `(emp_code, date)` — enforces concurrency safety
- `attendance_logs`: index on `(date, emp_code)` — serves `GET /attendance` sort
- `attendance_logs`: index on `(emp_code, date)` (already covered by unique index above) — serves attendance lookups

---

### Bug 20 — Missing endpoints
The starter is missing: `POST /attendance/punch-out`, `PATCH /attendance/{emp_code}/{date}`, all four analytics endpoints, and `GET /admin/explain/{endpoint}`.

---

## Not Defects

- **`IST = timezone(timedelta(hours=5, minutes=30))`** — correct constant, no bug.
- **`load_dotenv()`** at module level — correct; `.env` file is for local dev convenience, real env vars override it (that's how python-dotenv works).
- **`FastAPI(title=..., version=...)`** — matches the OpenAPI spec's `info` block.
- **`Optional[int] = None` for `punched_at`** in `PunchInIn` — structurally fine (the sentinel is handled), though the epoch-ms range validation is missing (separate bug).
