import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from app.main import app, db

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def clean_db():
    """Ensure clean collections for each test."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client


# ==============================================================================
# BRUTE-FORCE PYTHON REFERENCES (For Mathematical & Rule Verification)
# ==============================================================================

def round_half_up(val: Optional[Any], decimals: int) -> Optional[float]:
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def ref_employee_monthly(
    emp: dict,
    logs: list[dict],
    month_str: str,
) -> dict:
    year, m = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_d = date(year, m, 1)
    end_d = date(year, m, last_day)

    emp_joined = datetime.strptime(emp["joined_on"], "%Y-%m-%d").date()
    effective_start = max(emp_joined, start_d)

    # Mon-Fri working days
    if emp_joined <= end_d:
        working_days = 0
        cur = effective_start
        while cur <= end_d:
            if cur.weekday() < 5:
                working_days += 1
            cur += timedelta(days=1)
    else:
        working_days = 0

    # Aggregate matching logs
    emp_logs = [
        l for l in logs
        if l["emp_code"] == emp["emp_code"] and start_d.isoformat() <= l["date"] <= end_d.isoformat()
    ]

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late = 0
    total_ot = 0

    for l in emp_logs:
        l_date = datetime.strptime(l["date"], "%Y-%m-%d").date()
        is_weekday = l_date.weekday() < 5
        status = l.get("status")

        if status in ("PRESENT", "WFH", "ON_DUTY") and is_weekday:
            present_days += 0.5 if l.get("half_day") else 1.0

        if status == "LEAVE":
            leave_days += 1

        late_m = l.get("late_minutes") or 0
        if late_m > 0:
            late_count += 1
        total_late += late_m

        total_ot += l.get("overtime_minutes") or 0

    present_days = round_half_up(present_days, 2)
    attendance_pct = (
        round_half_up((present_days / working_days) * 100, 2)
        if working_days > 0 else None
    )

    return {
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def ref_department_summary(
    employees: list[dict],
    logs: list[dict],
    month_str: str,
    department: Optional[str] = None,
) -> list[dict]:
    year, m = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_d_str = f"{month_str}-01"
    end_d_str = f"{month_str}-{last_day:02d}"

    # Filter employees with joined_on <= end of period (R9)
    eligible_emps = [e for e in employees if e["joined_on"] <= end_d_str]
    if department:
        eligible_emps = [e for e in eligible_emps if e["department"] == department]

    # Group by department
    depts: dict[str, list[dict]] = {}
    for e in eligible_emps:
        depts.setdefault(e["department"], []).append(e)

    results = []
    for dept_name in sorted(depts.keys()):
        dept_emps = depts[dept_name]
        headcount = len(dept_emps)
        emp_codes = {e["emp_code"] for e in dept_emps}

        dept_logs = [
            l for l in logs
            if l["emp_code"] in emp_codes and start_d_str <= l["date"] <= end_d_str
        ]

        present_days = 0.0
        late_count = 0
        total_late = 0
        leave_count = 0
        on_duty_count = 0
        work_hours_list = []

        for l in dept_logs:
            l_date = datetime.strptime(l["date"], "%Y-%m-%d").date()
            is_weekday = l_date.weekday() < 5
            status = l.get("status")

            if status in ("PRESENT", "WFH", "ON_DUTY") and is_weekday:
                present_days += 0.5 if l.get("half_day") else 1.0

            if status == "LEAVE":
                leave_count += 1
            if status == "ON_DUTY":
                on_duty_count += 1

            late_m = l.get("late_minutes") or 0
            if late_m > 0:
                late_count += 1
            total_late += late_m

            if status in ("PRESENT", "WFH", "ON_DUTY") and l.get("work_hours") is not None:
                work_hours_list.append(l["work_hours"])

        avg_work_hours = (
            round_half_up(sum(work_hours_list) / len(work_hours_list), 2)
            if work_hours_list else None
        )

        results.append({
            "department": dept_name,
            "headcount": headcount,
            "present_days": round_half_up(present_days, 2),
            "avg_work_hours": avg_work_hours,
            "late_count": late_count,
            "total_late_minutes": total_late,
            "leave_count": leave_count,
            "on_duty_count": on_duty_count,
        })

    return results


def ref_late_leaderboard(
    employees: list[dict],
    logs: list[dict],
    month_str: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> list[dict]:
    year, m = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_d_str = f"{month_str}-01"
    end_d_str = f"{month_str}-{last_day:02d}"

    emp_map = {e["emp_code"]: e for e in employees}

    # Aggregate late minutes per employee
    emp_late_totals: dict[str, dict] = {}
    for l in logs:
        if not (start_d_str <= l["date"] <= end_d_str):
            continue
        code = l["emp_code"]
        if code not in emp_map:
            continue  # Ignore orphan logs
        late_m = l.get("late_minutes") or 0
        if late_m > 0:
            if code not in emp_late_totals:
                emp_late_totals[code] = {"total_late": 0, "late_count": 0}
            emp_late_totals[code]["total_late"] += late_m
            emp_late_totals[code]["late_count"] += 1

    # Filter by department BEFORE ranking
    rows = []
    for code, stats in emp_late_totals.items():
        emp = emp_map[code]
        if department and emp["department"] != department:
            continue
        rows.append({
            "emp_code": code,
            "name": emp["name"],
            "department": emp["department"],
            "total_late_minutes": stats["total_late"],
            "late_count": stats["late_count"],
        })

    # Sort descending by total late minutes, then emp_code ascending
    rows.sort(key=lambda r: (-r["total_late_minutes"], r["emp_code"]))

    # Standard competition ranking (1, 2, 2, 4)
    ranked_rows = []
    for i, r in enumerate(rows):
        if i == 0:
            rank = 1
        elif r["total_late_minutes"] == rows[i - 1]["total_late_minutes"]:
            rank = ranked_rows[i - 1]["rank"]
        else:
            rank = i + 1
        ranked_rows.append({**r, "rank": rank})

    # Filter rank <= limit AFTER ranking
    filtered = [r for r in ranked_rows if r["rank"] <= limit]
    return filtered


def ref_department_trend(
    employees: list[dict],
    logs: list[dict],
    department: str,
    from_str: str,
    to_str: str,
) -> list[dict]:
    d_from = datetime.strptime(from_str, "%Y-%m-%d").date()
    d_to = datetime.strptime(to_str, "%Y-%m-%d").date()

    dept_emps = [e for e in employees if e["department"] == department]
    dept_emp_codes = {e["emp_code"] for e in dept_emps}

    daily_rows = []
    cur = d_from
    while cur <= d_to:
        cur_str = cur.isoformat()
        is_working_day = cur.weekday() < 5
        headcount = sum(1 for e in dept_emps if e["joined_on"] <= cur_str)

        day_logs = [l for l in logs if l["emp_code"] in dept_emp_codes and l["date"] == cur_str]

        present_count = 0.0
        late_count = 0
        for l in day_logs:
            status = l.get("status")
            if status in ("PRESENT", "WFH", "ON_DUTY"):
                present_count += 0.5 if l.get("half_day") else 1.0
            if (l.get("late_minutes") or 0) > 0:
                late_count += 1

        present_count = round_half_up(present_count, 2) or 0.0

        if is_working_day and headcount > 0:
            rate = round_half_up(present_count / headcount, 4)
        else:
            rate = None

        daily_rows.append({
            "date": cur_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": rate,
        })
        cur += timedelta(days=1)

    # 7-day moving average (current + up to 6 preceding rows inside the range)
    for i in range(len(daily_rows)):
        window = daily_rows[max(0, i - 6): i + 1]
        valid_rates = [w["attendance_rate"] for w in window if w["attendance_rate"] is not None]
        if valid_rates:
            m_avg = round_half_up(sum(valid_rates) / len(valid_rates), 4)
        else:
            m_avg = None
        daily_rows[i]["moving_avg_7d"] = m_avg

    return daily_rows


# ==============================================================================
# TESTS: CORE CRUD, HEALTH, AND ACTIONS
# ==============================================================================

def test_health_check(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_employee_creation_and_rejections(client):
    # 1. Valid creation
    emp_payload = {
        "emp_code": "EMP5001",
        "name": "John Doe",
        "email": "john@company.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-15",
    }
    r = client.post("/employees", json=emp_payload)
    assert r.status_code == 201
    data = r.json()
    assert "_id" not in data
    assert isinstance(data["created_at"], int)
    assert data["emp_code"] == "EMP5001"

    # 2. 409 Duplicate emp_code
    r = client.post("/employees", json=emp_payload)
    assert r.status_code == 409

    # 3. 422 Identical shift_start and shift_end
    bad_shift = {**emp_payload, "emp_code": "EMP5002", "shift_end": "09:30"}
    r = client.post("/employees", json=bad_shift)
    assert r.status_code == 422

    # 4. 422 Invalid emp_code format
    bad_code = {**emp_payload, "emp_code": "INVALID12"}
    r = client.post("/employees", json=bad_code)
    assert r.status_code == 422

    # 5. 422 Invalid calendar date
    bad_date = {**emp_payload, "emp_code": "EMP5003", "joined_on": "2026-02-30"}
    r = client.post("/employees", json=bad_date)
    assert r.status_code == 422


def test_overnight_shift_punch_in_and_punch_out(client):
    # Security guard on overnight shift: 22:00 to 06:00
    emp = {
        "emp_code": "EMP5010",
        "name": "Night Guard",
        "email": "guard@company.com",
        "department": "Security",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01",
    }
    client.post("/employees", json=emp)

    # Punches in on 2026-03-02 at 01:15:00 AM IST (past midnight)
    # Per R1: time 01:15 < shift_end 06:00 -> belongs to previous day's shift: 2026-03-01!
    # Shift start: 2026-03-01 22:00 IST.
    # Late arrival: 3 hours 15 min = 195 min late (> 600s grace)
    t_in = int(datetime(2026, 3, 2, 1, 15, 0, tzinfo=IST).timestamp()) * 1000
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP5010", "punched_at": t_in})
    assert r.status_code == 201
    data = r.json()
    assert data["date"] == "2026-03-01"
    assert data["late_minutes"] == 195
    assert data["work_hours"] is None

    # Punch out at 2026-03-02 06:45:00 AM IST
    # Duration: 01:15 to 06:45 = 5.50 hrs.
    # Shift end: 2026-03-02 06:00 IST. Punch-out 06:45 = 45 min overtime (>= 30 min -> 45)
    t_out = int(datetime(2026, 3, 2, 6, 45, 0, tzinfo=IST).timestamp()) * 1000
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP5010", "punched_at": t_out})
    assert r.status_code == 200
    data = r.json()
    assert data["date"] == "2026-03-01"
    assert data["work_hours"] == 5.50
    assert data["overtime_minutes"] == 45
    assert data["half_day"] is False

    # Second punch-out attempt -> 409
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP5010", "punched_at": t_out})
    assert r.status_code == 409


def test_strict_grace_period_rules(client):
    emp = {
        "emp_code": "EMP5020",
        "name": "Grace Test",
        "email": "grace@test.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    client.post("/employees", json=emp)

    # Case 1: Punch in at exactly 09:40:00 IST (exactly 600s after shift start) -> 0 late
    t_094000 = int(datetime(2026, 3, 10, 9, 40, 0, tzinfo=IST).timestamp()) * 1000
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP5020", "punched_at": t_094000})
    assert r.status_code == 201
    assert r.json()["late_minutes"] == 0

    # Case 2: Punch in at 09:40:01 IST (601s strictly > 600s) on next day -> 10 late
    t_094001 = int(datetime(2026, 3, 11, 9, 40, 1, tzinfo=IST).timestamp()) * 1000
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP5020", "punched_at": t_094001})
    assert r.status_code == 201
    assert r.json()["late_minutes"] == 10

    # Case 3: Punch in at 10:15:59 IST (2759s) on next day -> 45 late
    t_101559 = int(datetime(2026, 3, 12, 10, 15, 59, tzinfo=IST).timestamp()) * 1000
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP5020", "punched_at": t_101559})
    assert r.status_code == 201
    assert r.json()["late_minutes"] == 45


def test_attendance_patch_regularization(client):
    emp = {
        "emp_code": "EMP5030",
        "name": "Patch Test",
        "email": "patch@test.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01",
    }
    client.post("/employees", json=emp)

    t_in = int(datetime(2026, 3, 15, 9, 30, 0, tzinfo=IST).timestamp()) * 1000
    client.post("/attendance/punch-in", json={"emp_code": "EMP5030", "punched_at": t_in})

    # 1. 422 when request changes nothing
    r = client.patch("/attendance/EMP5030/2026-03-15", json={
        "status": "PRESENT",
        "reason": "Nothing changed",
        "regularized_by": "Manager",
    })
    assert r.status_code == 422

    # 2. Add punch_out via PATCH (work_hours = 9.0, overtime = 0)
    t_out = int(datetime(2026, 3, 15, 18, 30, 0, tzinfo=IST).timestamp()) * 1000
    r = client.patch("/attendance/EMP5030/2026-03-15", json={
        "punch_out": t_out,
        "reason": "Forgot to punch out",
        "regularized_by": "Manager",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["work_hours"] == 9.0
    assert len(data["history"]) == 1
    assert "punch_out" in data["history"][0]["changes"]
    assert data["history"][0]["changes"]["punch_out"]["to"] == t_out

    # 3. Regularize to LEAVE: clears punches, work_hours None, half_day False
    r = client.patch("/attendance/EMP5030/2026-03-15", json={
        "status": "LEAVE",
        "reason": "Applied leave after the fact",
        "regularized_by": "HR Admin",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "LEAVE"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert len(data["history"]) == 2

    # 4. Supplying punches with LEAVE -> 422
    r = client.patch("/attendance/EMP5030/2026-03-15", json={
        "status": "LEAVE",
        "punch_in": t_in,
        "reason": "Invalid payload",
        "regularized_by": "HR Admin",
    })
    assert r.status_code == 422


# ==============================================================================
# TESTS: COMPLEX ANALYTICS SCENARIOS VS BRUTE-FORCE PYTHON REFERENCES
# ==============================================================================

def test_comprehensive_analytics_against_python_references(client):
    """
    Complex end-to-end benchmark containing:
    1. Ties on leaderboard
    2. Weekend records (Saturday/Sunday)
    3. A no-log employee
    4. A mid-month joiner
    5. An overnight shift employee
    6. A missing weekday
    """
    # 1. EMPLOYEES SETUP
    employees_data = [
        # Normal employee, Engineering, joined Jan 1
        {
            "emp_code": "EMP6001",
            "name": "Alice Eng",
            "email": "alice@test.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
        # Mid-month joiner, Engineering, joined March 16 (Monday)
        {
            "emp_code": "EMP6002",
            "name": "Bob MidMonth",
            "email": "bob@test.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-03-16",
        },
        # No-log employee, Engineering, joined Feb 1
        {
            "emp_code": "EMP6003",
            "name": "Charlie NoLog",
            "email": "charlie@test.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-02-01",
        },
        # Future joiner, Engineering, joined April 1 (excluded from March headcount)
        {
            "emp_code": "EMP6004",
            "name": "Dave Future",
            "email": "dave@test.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-04-01",
        },
        # Overnight shift employee, Support, joined Jan 1
        {
            "emp_code": "EMP7001",
            "name": "Oliver Overnight",
            "email": "oliver@test.com",
            "department": "Support",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-01-01",
        },
        # Sales employee for tie testing, Sales, joined Jan 1
        {
            "emp_code": "EMP8001",
            "name": "Sarah Sales",
            "email": "sarah@test.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        },
    ]

    for emp in employees_data:
        client.post("/employees", json=emp)

    # 2. LOGS SETUP (March 2026)
    # Alice (EMP6001):
    # - 2026-03-02 (Mon): PRESENT, 8.0 hrs, late 25m, ot 30m, half_day False
    # - 2026-03-03 (Tue): PRESENT, 4.0 hrs, late 15m, ot 0m, half_day True (half day = 0.5)
    # - 2026-03-04 (Wed): MISSING WEEKDAY! (No record for anyone in Eng)
    # - 2026-03-05 (Thu): LEAVE
    # - 2026-03-07 (Sat): WEEKEND RECORD! PRESENT, 6.0 hrs, late 20m, ot 60m
    # Total late for Alice: 25 + 15 + 20 = 60m. Late count = 3.

    # Bob (EMP6002 - mid-month joiner):
    # - 2026-03-16 (Mon): PRESENT, 9.0 hrs, late 0m, ot 30m, half_day False

    # Oliver (EMP7001 - Overnight):
    # - Punched in 2026-03-02 01:00 AM IST -> record date 2026-03-01 (Sunday weekend per R1)
    #   Late minutes = 180m, punched out 07:00 AM (60m overtime)

    # Sarah (EMP8001 - Sales):
    # - 2026-03-02 (Mon): PRESENT, 8.0 hrs, late 60m (Ties with Alice at 60m late!)

    t_alice_mon_in = int(datetime(2026, 3, 2, 9, 55, 0, tzinfo=IST).timestamp()) * 1000
    t_alice_mon_out = int(datetime(2026, 3, 2, 19, 0, 0, tzinfo=IST).timestamp()) * 1000
    client.post("/attendance/punch-in", json={"emp_code": "EMP6001", "punched_at": t_alice_mon_in})
    client.post("/attendance/punch-out", json={"emp_code": "EMP6001", "punched_at": t_alice_mon_out})

    t_alice_tue_in = int(datetime(2026, 3, 3, 9, 45, 0, tzinfo=IST).timestamp()) * 1000
    t_alice_tue_out = int(datetime(2026, 3, 3, 13, 45, 0, tzinfo=IST).timestamp()) * 1000
    client.post("/attendance/punch-in", json={"emp_code": "EMP6001", "punched_at": t_alice_tue_in})
    client.post("/attendance/punch-out", json={"emp_code": "EMP6001", "punched_at": t_alice_tue_out})

    # Alice Thursday Leave
    t_alice_thu_in = int(datetime(2026, 3, 5, 9, 30, 0, tzinfo=IST).timestamp()) * 1000
    client.post("/attendance/punch-in", json={"emp_code": "EMP6001", "punched_at": t_alice_thu_in})
    client.patch("/attendance/EMP6001/2026-03-05", json={"status": "LEAVE", "reason": "Sick day", "regularized_by": "HR"})

    # Alice Saturday Weekend
    t_alice_sat_in = int(datetime(2026, 3, 7, 9, 50, 0, tzinfo=IST).timestamp()) * 1000
    t_alice_sat_out = int(datetime(2026, 3, 7, 19, 30, 0, tzinfo=IST).timestamp()) * 1000
    client.post("/attendance/punch-in", json={"emp_code": "EMP6001", "punched_at": t_alice_sat_in})
    client.post("/attendance/punch-out", json={"emp_code": "EMP6001", "punched_at": t_alice_sat_out})

    # Bob (mid-month joiner)
    t_bob_in = int(datetime(2026, 3, 16, 9, 30, 0, tzinfo=IST).timestamp()) * 1000
    t_bob_out = int(datetime(2026, 3, 16, 19, 0, 0, tzinfo=IST).timestamp()) * 1000
    client.post("/attendance/punch-in", json={"emp_code": "EMP6002", "punched_at": t_bob_in})
    client.post("/attendance/punch-out", json={"emp_code": "EMP6002", "punched_at": t_bob_out})

    # Oliver overnight
    t_oliver_in = int(datetime(2026, 3, 2, 1, 0, 0, tzinfo=IST).timestamp()) * 1000
    t_oliver_out = int(datetime(2026, 3, 2, 7, 0, 0, tzinfo=IST).timestamp()) * 1000
    client.post("/attendance/punch-in", json={"emp_code": "EMP7001", "punched_at": t_oliver_in})
    client.post("/attendance/punch-out", json={"emp_code": "EMP7001", "punched_at": t_oliver_out})

    # Sarah (Sales) - 60 min late
    t_sarah_in = int(datetime(2026, 3, 2, 10, 30, 0, tzinfo=IST).timestamp()) * 1000
    t_sarah_out = int(datetime(2026, 3, 2, 18, 30, 0, tzinfo=IST).timestamp()) * 1000
    client.post("/attendance/punch-in", json={"emp_code": "EMP8001", "punched_at": t_sarah_in})
    client.post("/attendance/punch-out", json={"emp_code": "EMP8001", "punched_at": t_sarah_out})

    # Fetch all stored records from DB to supply to Python reference functions
    all_stored_logs = list(db.attendance_logs.find({}, {"_id": 0}))

    # --------------------------------------------------------------------------
    # COMPARISON 1: GET /analytics/employees/{emp_code}/monthly
    # --------------------------------------------------------------------------
    # Test Alice (Normal + Weekend record)
    api_alice = client.get("/analytics/employees/EMP6001/monthly?month=2026-03").json()
    ref_alice = ref_employee_monthly(employees_data[0], all_stored_logs, "2026-03")
    assert api_alice == ref_alice, f"Alice monthly mismatch: {api_alice} vs {ref_alice}"

    # Test Bob (Mid-month joiner)
    api_bob = client.get("/analytics/employees/EMP6002/monthly?month=2026-03").json()
    ref_bob = ref_employee_monthly(employees_data[1], all_stored_logs, "2026-03")
    assert api_bob == ref_bob, f"Bob monthly mismatch: {api_bob} vs {ref_bob}"

    # Test Charlie (No-log employee)
    api_charlie = client.get("/analytics/employees/EMP6003/monthly?month=2026-03").json()
    ref_charlie = ref_employee_monthly(employees_data[2], all_stored_logs, "2026-03")
    assert api_charlie == ref_charlie, f"Charlie monthly mismatch: {api_charlie} vs {ref_charlie}"

    # --------------------------------------------------------------------------
    # COMPARISON 2: GET /analytics/departments/summary
    # --------------------------------------------------------------------------
    api_summary = client.get("/analytics/departments/summary?month=2026-03").json()
    ref_summary = ref_department_summary(employees_data, all_stored_logs, "2026-03")
    assert api_summary == ref_summary, f"Department summary mismatch: {api_summary} vs {ref_summary}"

    # Verify Engineering headcount includes Charlie (no-log) and excludes Dave (future joiner)
    eng_summary = next(s for s in api_summary if s["department"] == "Engineering")
    assert eng_summary["headcount"] == 3  # Alice, Bob, Charlie (Dave excluded)

    # --------------------------------------------------------------------------
    # COMPARISON 3: GET /analytics/leaderboard/late (Testing Ties & Ranking)
    # --------------------------------------------------------------------------
    api_lb = client.get("/analytics/leaderboard/late?month=2026-03&limit=5").json()
    ref_lb = ref_late_leaderboard(employees_data, all_stored_logs, "2026-03", limit=5)
    assert api_lb == ref_lb, f"Late leaderboard mismatch: {api_lb} vs {ref_lb}"

    # Verify tie: Alice (EMP6001) and Sarah (EMP8001) both have 60 min late and rank 2
    # Oliver (EMP7001) has 180 min late and rank 1
    assert api_lb[0]["emp_code"] == "EMP7001"
    assert api_lb[0]["rank"] == 1
    assert api_lb[1]["rank"] == 2
    assert api_lb[2]["rank"] == 2

    # --------------------------------------------------------------------------
    # COMPARISON 4: GET /analytics/departments/{department}/trend (Densify, Missing Weekday)
    # --------------------------------------------------------------------------
    api_trend = client.get("/analytics/departments/Engineering/trend?from=2026-03-01&to=2026-03-06").json()
    ref_trend = ref_department_trend(employees_data, all_stored_logs, "Engineering", "2026-03-01", "2026-03-06")
    assert api_trend == ref_trend, f"Department trend mismatch: {api_trend} vs {ref_trend}"

    # Verify Wednesday 2026-03-04 (missing weekday) in API trend:
    wed_row = next(r for r in api_trend if r["date"] == "2026-03-04")
    assert wed_row["is_working_day"] is True
    assert wed_row["present_count"] == 0.0
    assert wed_row["attendance_rate"] == 0.0


# ==============================================================================
# TESTS: ADMIN EXPLAIN ENDPOINTS
# ==============================================================================

def test_explain_endpoints_plans_and_validations(client):
    # 1. Invalid endpoint -> 422
    r = client.get("/admin/explain/unknown")
    assert r.status_code == 422

    # 2. attendance_list explain
    r = client.get("/admin/explain/attendance_list")
    assert r.status_code == 200
    assert r.json()["collection"] == "attendance_logs"

    # 3. employee_monthly explain
    r = client.get("/admin/explain/employee_monthly?emp_code=EMP6001&month=2026-03")
    assert r.status_code == 200
    assert r.json()["collection"] == "attendance_logs"

    # 4. department_summary explain
    r = client.get("/admin/explain/department_summary?month=2026-03")
    assert r.status_code == 200
    assert r.json()["collection"] == "employees"

    # 5. late_leaderboard explain
    r = client.get("/admin/explain/late_leaderboard?month=2026-03&limit=5")
    assert r.status_code == 200
    assert r.json()["collection"] == "attendance_logs"

    # 6. department_trend explain
    r = client.get("/admin/explain/department_trend?department=Engineering&from=2026-03-01&to=2026-03-10")
    assert r.status_code == 200
    assert r.json()["collection"] == "employees"
