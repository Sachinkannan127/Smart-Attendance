import calendar
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Dict, List, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# ==============================================================================
# 1. CONFIG & ENVIRONMENT
# ==============================================================================

# python-dotenv: real environment variables must win (override=False)
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("attendance_api")

# PyMongo sync client with tz_aware=True and short server selection timeout
client: MongoClient = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

# Regex patterns
EMP_CODE_REGEX = re.compile(r"^EMP\d{4,6}$")
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TIME_REGEX = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


# ==============================================================================
# 2. LIFESPAN & APP SETUP
# ==============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Ping MongoDB and log connection status
    try:
        ping_res = client.admin.command("ping")
        logger.info(f"MongoDB connection verified successfully: ping command returned {ping_res} (db='{MONGO_DB}')")
    except Exception as exc:
        logger.error(f"MongoDB connection ping failed during startup: {exc}")

    # 2. Create indexes idempotently at startup
    try:
        # employees indexes
        db.employees.create_index([("emp_code", 1)], unique=True, name="uniq_emp_code")
        db.employees.create_index([("department", 1), ("emp_code", 1)], name="idx_dept_emp_code")
        db.employees.create_index([("department", 1), ("joined_on", 1)], name="idx_dept_joined_on")
        db.employees.create_index([("joined_on", 1)], name="idx_joined_on")

        # attendance_logs indexes
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uniq_emp_code_date")
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="idx_date_desc_emp_code")
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)], name="idx_date_late_emp")
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="idx_emp_punch_in_desc")
        logger.info("MongoDB indexes verified / created successfully.")
    except PyMongoError as exc:
        logger.error(f"Error creating MongoDB indexes: {exc}")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", lifespan=lifespan)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(RequestValidationError)
def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(exc.errors())},
    )


@app.exception_handler(HTTPException)
def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


@app.exception_handler(PyMongoError)
def mongo_exception_handler(request: Request, exc: PyMongoError):
    # Map real database connection/runtime errors to 503 Service Unavailable
    logger.error(f"MongoDB error on {request.method} {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Database unavailable"},
    )


# ==============================================================================
# 3. PURE HELPER FUNCTIONS (Business Rules R1 - R5)
# ==============================================================================

def ms_to_datetime_truncated(ms: int) -> datetime:
    """
    R1: Truncate instants to whole seconds first; store the truncated UTC value.
    """
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def datetime_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """
    Convert datetime to integer epoch milliseconds truncated to whole seconds.
    """
    if dt is None:
        return None
    return int(dt.timestamp()) * 1000


def parse_time_hm(time_str: str) -> tuple[int, int]:
    h, m = map(int, time_str.split(":"))
    return h, m


def get_record_date_for_punch_in(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 IST (UTC+05:30). Record date = IST date of punch_in.
    Overnight shift (shift_end <= shift_start): a punch_in whose IST time-of-day is earlier
    than shift_end belongs to the previous day's shift; the shift end is on the next calendar day.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    start_h, start_m = parse_time_hm(shift_start)
    end_h, end_m = parse_time_hm(shift_end)
    is_overnight = (end_h, end_m) <= (start_h, start_m)

    punch_time = (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second)
    shift_end_time = (end_h, end_m, 0)

    if is_overnight and punch_time < shift_end_time:
        rec_date = punch_in_ist.date() - timedelta(days=1)
    else:
        rec_date = punch_in_ist.date()
    return rec_date.isoformat()


def get_shift_datetimes(record_date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """
    Compute shift_start and shift_end datetimes in IST for a given attendance record date.
    For overnight shifts, shift_end is anchored to (record_date + 1 day).
    """
    rec_date = datetime.strptime(record_date_str, "%Y-%m-%d").date()
    start_h, start_m = parse_time_hm(shift_start)
    end_h, end_m = parse_time_hm(shift_end)

    start_dt = datetime(rec_date.year, rec_date.month, rec_date.day, start_h, start_m, 0, tzinfo=IST)
    if (end_h, end_m) <= (start_h, start_m):
        end_date = rec_date + timedelta(days=1)
        end_dt = datetime(end_date.year, end_date.month, end_date.day, end_h, end_m, 0, tzinfo=IST)
    else:
        end_dt = datetime(rec_date.year, rec_date.month, rec_date.day, end_h, end_m, 0, tzinfo=IST)
    return start_dt, end_dt


def calculate_late_minutes(punch_in_dt: Optional[datetime], record_date_str: str, shift_start: str, shift_end: str) -> int:
    """
    R2 Late: grace 10 min. Late only if punch_in is STRICTLY more than 600 s after shift_start;
    then late_minutes = floor(minutes since shift_start) (not since grace end), else 0.
    """
    if punch_in_dt is None:
        return 0
    shift_start_dt, _ = get_shift_datetimes(record_date_str, shift_start, shift_end)
    punch_in_ist = punch_in_dt.astimezone(IST)
    diff_sec = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def calculate_overtime_minutes(punch_out_dt: Optional[datetime], record_date_str: str, shift_start: str, shift_end: str) -> int:
    """
    R3 Overtime: floor minutes from shift_end to punch_out, only if >= 30, else 0.
    """
    if punch_out_dt is None:
        return 0
    _, shift_end_dt = get_shift_datetimes(record_date_str, shift_start, shift_end)
    punch_out_ist = punch_out_dt.astimezone(IST)
    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def calculate_work_hours(punch_in_dt: Optional[datetime], punch_out_dt: Optional[datetime]) -> Optional[float]:
    """
    R4 work_hours = (punch_out - punch_in) seconds / 3600, rounded to 2 decimals HALF-UP
    (use Decimal, never round()). null until punch-out.
    """
    if punch_in_dt is None or punch_out_dt is None:
        return None
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec < 0:
        return None
    hours = (Decimal(str(diff_sec)) / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def calculate_half_day(work_hours: Optional[float]) -> bool:
    """
    R5 half_day = rounded work_hours < 4.50 after punch-out; a half day counts as 0.5 present day.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def round_half_up(val: Optional[Any], decimals: int) -> Optional[float]:
    """
    R8 Reported numbers: 2 decimals half-up; rates 4 decimals half-up.
    Uses Python Decimal with ROUND_HALF_UP (never banker's round()).
    """
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def get_month_bounds(month_str: str) -> tuple[str, str, date, date]:
    """
    Validate month format 'YYYY-MM' and return start and end date strings and date objects.
    """
    if not MONTH_REGEX.match(month_str):
        raise HTTPException(status_code=422, detail="month must match pattern YYYY-MM")
    year, m = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(year, m)
    start_d = date(year, m, 1)
    end_d = date(year, m, last_day)
    return start_d.isoformat(), end_d.isoformat(), start_d, end_d


def count_working_days(start_d: date, end_d: date) -> int:
    """
    R7: Working days = Mon-Fri, no holidays.
    Count number of Mon-Fri calendar days in [start_d, end_d] inclusive.
    """
    if start_d > end_d:
        return 0
    cur = start_d
    count = 0
    while cur <= end_d:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count


def serialize_employee_doc(doc: dict) -> dict:
    """
    Strip _id and convert created_at datetime to integer epoch ms.
    """
    res = dict(doc)
    res.pop("_id", None)
    if isinstance(res.get("created_at"), datetime):
        res["created_at"] = datetime_to_ms(res["created_at"])
    return res


def serialize_attendance_doc(doc: dict) -> dict:
    """
    Strip _id, convert datetimes to epoch ms (boundary conversion including inside history[].changes),
    and handle legacy default fields (half_day=false, history=[]).
    """
    res = dict(doc)
    res.pop("_id", None)

    if isinstance(res.get("punch_in"), datetime):
        res["punch_in"] = datetime_to_ms(res["punch_in"])
    if isinstance(res.get("punch_out"), datetime):
        res["punch_out"] = datetime_to_ms(res["punch_out"])

    if "half_day" not in res or res["half_day"] is None:
        res["half_day"] = False
    if "history" not in res or res["history"] is None:
        res["history"] = []
    else:
        clean_history = []
        for h in res["history"]:
            item = dict(h)
            if isinstance(item.get("at"), datetime):
                item["at"] = datetime_to_ms(item["at"])
            changes = item.get("changes", {})
            clean_changes = {}
            for field, change in changes.items():
                if isinstance(change, dict):
                    c = dict(change)
                    if field in ("punch_in", "punch_out"):
                        if isinstance(c.get("from"), datetime):
                            c["from"] = datetime_to_ms(c["from"])
                        if isinstance(c.get("to"), datetime):
                            c["to"] = datetime_to_ms(c["to"])
                    clean_changes[field] = c
                else:
                    clean_changes[field] = change
            item["changes"] = clean_changes
            clean_history.append(item)
        res["history"] = clean_history

    return res


# ==============================================================================
# 4. PYDANTIC MODELS
# ==============================================================================

# StrictInt: 100000000000 <= v <= 4102444800000; reject floats, strings, bools, seconds
StrictEpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., description="Unique employee code, e.g. EMP1001")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., description="Employee email address")
    department: str = Field(..., min_length=1, max_length=100)
    shift_start: str = Field(default="09:30")
    shift_end: str = Field(default="18:30")
    joined_on: str = Field(..., description="YYYY-MM-DD")

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        if not EMP_CODE_REGEX.match(v):
            raise ValueError("emp_code must match pattern ^EMP\\d{4,6}$")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not EMAIL_REGEX.match(v):
            raise ValueError("Invalid email format")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, v: str) -> str:
        if not TIME_REGEX.match(v):
            raise ValueError("Time must be in HH:MM format (00:00 to 23:59)")
        return v

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        if not DATE_REGEX.match(v):
            raise ValueError("joined_on must match YYYY-MM-DD format")
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Invalid calendar date for joined_on")
        return v

    @model_validator(mode="after")
    def validate_shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self

    model_config = {
        "json_schema_extra": {
            "example": {
                "emp_code": "EMP1001",
                "name": "Alice Smith",
                "email": "alice@example.com",
                "department": "Engineering",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-15",
            }
        }
    }


class EmployeeResponse(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class PaginatedEmployeesResponse(BaseModel):
    items: List[EmployeeResponse]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictEpochMs] = None
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY"]] = "PRESENT"

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        if not EMP_CODE_REGEX.match(v):
            raise ValueError("emp_code must match pattern ^EMP\\d{4,6}$")
        return v

    model_config = {
        "json_schema_extra": {
            "example": {
                "emp_code": "EMP1001",
                "status": "PRESENT",
            }
        }
    }


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictEpochMs] = None

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, v: str) -> str:
        if not EMP_CODE_REGEX.match(v):
            raise ValueError("emp_code must match pattern ^EMP\\d{4,6}$")
        return v

    model_config = {
        "json_schema_extra": {
            "example": {
                "emp_code": "EMP1001",
            }
        }
    }


class AttendancePatchRequest(BaseModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictEpochMs] = None
    punch_out: Optional[StrictEpochMs] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "PRESENT",
                "reason": "Corrected missed punch by manager",
                "regularized_by": "MGR001",
            }
        }
    }


class HistoryItem(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Dict[str, Any]]


class AttendanceResponse(BaseModel):
    emp_code: str
    date: str
    status: str
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    work_hours: Optional[float] = None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryItem] = []


class PaginatedAttendanceResponse(BaseModel):
    items: List[AttendanceResponse]
    total: int
    page: int
    page_size: int


class EmployeeMonthlyAnalyticsResponse(BaseModel):
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class LateLeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class DepartmentTrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


# ==============================================================================
# 5. QUERY BUILDERS
# ==============================================================================

def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    """
    Shared query builder for GET /attendance and GET /admin/explain/attendance_list
    """
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status
    if date_from and date_to:
        query["date"] = {"$gte": date_from, "$lte": date_to}
    elif date_from:
        query["date"] = {"$gte": date_from}
    elif date_to:
        query["date"] = {"$lte": date_to}
    return query


def build_employee_monthly_pipeline(
    emp_code: str,
    start_date_str: str,
    end_date_str: str,
) -> list[dict[str, Any]]:
    """
    Shared pipeline builder for GET /analytics/employees/{emp_code}/monthly
    and GET /admin/explain/employee_monthly
    """
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date_str, "$lte": end_date_str},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": ["$day_of_week", 5]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


def build_department_summary_pipeline(
    start_date_str: str,
    end_date_str: str,
    department: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Shared pipeline builder for GET /analytics/departments/summary
    and GET /admin/explain/department_summary
    """
    match_filter: dict[str, Any] = {"joined_on": {"$lte": end_date_str}}
    if department:
        match_filter["department"] = department

    return [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date_str]},
                                    {"$lte": ["$date", end_date_str]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_present_days": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$this.date"}}}, 5]},
                                            ]
                                        },
                                        {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "emp_late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "emp_total_late_minutes": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {"$add": ["$$value", {"$ifNull": ["$$this.late_minutes", 0]}]},
                    }
                },
                "emp_leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
                "emp_work_hours_sum": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$$this.work_hours", None]},
                                            ]
                                        },
                                        "$$this.work_hours",
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "emp_work_hours_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {
                                "$and": [
                                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$$l.work_hours", None]},
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "total_work_hours": {"$sum": "$emp_work_hours_sum"},
                "total_work_hours_count": {"$sum": "$emp_work_hours_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(
    start_date_str: str,
    end_date_str: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Shared pipeline builder for GET /analytics/leaderboard/late
    and GET /admin/explain/late_leaderboard
    """
    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date_str, "$lte": end_date_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
    ])
    return pipeline


def build_department_trend_pipeline(
    department: str,
    date_from_str: str,
    date_to_str: str,
) -> list[dict[str, Any]]:
    """
    Shared pipeline builder for GET /analytics/departments/{department}/trend
    and GET /admin/explain/department_trend
    """
    start_dt = datetime.strptime(date_from_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = (datetime.strptime(date_to_str, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=timezone.utc)

    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"date": {"$literal": start_dt}}},
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_dt],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "day_of_week": {"$isoDayOfWeek": "$date"},
            }
        },
        {
            "$set": {
                "is_working_day": {"$lte": ["$day_of_week", 5]}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]}
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$d"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp",
                        }
                    },
                    {"$unwind": "$emp"},
                    {
                        "$match": {
                            "emp.department": department
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


# ==============================================================================
# 5. ENDPOINTS (Stage 1)
# ==============================================================================

@app.get("/", response_class=FileResponse)
def serve_frontend():
    """
    GET /: Serves the Smart Attendance dashboard UI.
    """
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "message": "Smart Attendance API is running. Visit /docs for OpenAPI documentation."},
    )


@app.get("/health")
def health():
    """
    GET /health returns 200 {"status":"ok"} only if Mongo ping succeeds, else 503.
    """
    try:
        ping_res = client.admin.command("ping")
        logger.info(f"MongoDB ping check succeeded: {ping_res}")
        return {"status": "ok"}
    except Exception as exc:
        logger.warning(f"Health check failed: {exc}")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "error", "detail": "Database unavailable"},
        )


@app.post("/employees", status_code=status.HTTP_201_CREATED, response_model=EmployeeResponse)
def create_employee(payload: EmployeeCreate):
    """
    POST /employees: 201; 409 on duplicate emp_code enforced by UNIQUE INDEX
    (catch DuplicateKeyError, no check-then-insert); 422 invalid; returns created_at as epoch ms.
    """
    now_sec = int(time.time())
    created_at_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    doc = payload.model_dump()
    doc["created_at"] = created_at_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Employee with code '{payload.emp_code}' already exists",
        )

    return serialize_employee_doc(doc)


@app.get("/employees", response_model=PaginatedEmployeesResponse)
def get_employees(
    department: Optional[str] = Query(None, description="Department name (exact match)"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Page size"),
):
    """
    GET /employees: optional department (exact), sorted by emp_code, paginated, total = filtered count.
    """
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)
    skip = (page - 1) * page_size
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)

    items = [serialize_employee_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, response_model=AttendanceResponse)
def punch_in(payload: PunchInRequest):
    """
    POST /attendance/punch-in {emp_code, punched_at?, status?}:
    only PRESENT/WFH/ON_DUTY; 404 unknown employee; 409 duplicate (unique index on (emp_code,date),
    insert and catch DuplicateKeyError; exactly one 201 under concurrency); 422 invalid.
    Initial doc: punch_out null, work_hours null, overtime 0, half_day false, history [].
    """
    emp = db.employees.find_one({"emp_code": payload.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{payload.emp_code}' not found",
        )

    if payload.punched_at is not None:
        punch_in_dt = ms_to_datetime_truncated(payload.punched_at)
    else:
        now_sec = int(time.time())
        punch_in_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    record_date = get_record_date_for_punch_in(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = calculate_late_minutes(punch_in_dt, record_date, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": payload.emp_code,
        "date": record_date,
        "status": payload.status or "PRESENT",
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail=f"Attendance record for employee '{payload.emp_code}' on date '{record_date}' already exists",
        )

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", response_model=AttendanceResponse)
def punch_out(payload: PunchOutRequest):
    """
    POST /attendance/punch-out {emp_code, punched_at?}:
    find the employee's most recent record with punch_in <= punched_at (works across midnight).
    404 unknown employee or no punch-in;
    409 if already punched out (atomic update_one with punch_out:None in the filter; zero matches -> 409; exactly one 200 under concurrency);
    422 if punched_at <= punch_in or > 24h after it. Compute R3-R5.
    """
    emp = db.employees.find_one({"emp_code": payload.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee '{payload.emp_code}' not found")

    if payload.punched_at is not None:
        punch_out_dt = ms_to_datetime_truncated(payload.punched_at)
    else:
        now_sec = int(time.time())
        punch_out_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    # Find the employee's most recent record with punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {
            "emp_code": payload.emp_code,
            "punch_in": {"$lte": punch_out_dt},
        },
        {"_id": 0},
        sort=[("punch_in", -1)],
    )

    if not record or record.get("punch_in") is None:
        raise HTTPException(status_code=404, detail="No punch-in record found for employee")

    punch_in_dt = record["punch_in"]
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be strictly greater than punch_in")
    if diff_sec > 86400:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    # Compute R3-R5
    work_hours = calculate_work_hours(punch_in_dt, punch_out_dt)
    overtime_min = calculate_overtime_minutes(punch_out_dt, record["date"], emp["shift_start"], emp["shift_end"])
    half_day = calculate_half_day(work_hours)

    # Atomic update with punch_out: None to guarantee concurrency safety
    res = db.attendance_logs.update_one(
        {
            "emp_code": payload.emp_code,
            "date": record["date"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
    )

    if res.matched_count == 0:
        raise HTTPException(status_code=409, detail="Employee has already punched out for this shift")

    updated = db.attendance_logs.find_one({"emp_code": payload.emp_code, "date": record["date"]}, {"_id": 0})
    return serialize_attendance_doc(updated)


@app.get("/attendance", response_model=PaginatedAttendanceResponse)
def get_attendance(
    emp_code: Optional[str] = Query(None, description="Employee code filter"),
    date_from: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = Query(None, description="Status filter"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Page size"),
):
    """
    GET /attendance: filters emp_code, date_from, date_to (inclusive; from > to -> 422), status;
    sort date desc then emp_code asc; paginated; DB-side sort/skip/limit only; must be fast at 100k docs.
    """
    if emp_code is not None and not EMP_CODE_REGEX.match(emp_code):
        raise HTTPException(status_code=422, detail="emp_code must match pattern ^EMP\\d{4,6}$")
    if date_from is not None:
        if not DATE_REGEX.match(date_from):
            raise HTTPException(status_code=422, detail="date_from must match YYYY-MM-DD format")
        try:
            datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid calendar date for date_from")
    if date_to is not None:
        if not DATE_REGEX.match(date_to):
            raise HTTPException(status_code=422, detail="date_to must match YYYY-MM-DD format")
        try:
            datetime.strptime(date_to, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid calendar date for date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be less than or equal to date_to")

    query = build_attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .hint("idx_date_desc_emp_code")
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_attendance_doc(doc) for doc in cursor]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceResponse)
def patch_attendance(
    emp_code: str,
    date: str,
    payload: AttendancePatchRequest,
):
    """
    PATCH /attendance/{emp_code}/{date}:
    body {status?, punch_in?, punch_out?, reason (5-200 chars), regularized_by (1-50 chars)}.
    Omitted fields unchanged. ABSENT/LEAVE clears both punches (supplying punches with them -> 422).
    Presence status requires punch_in (supplied or stored). punch_in must stay on the record's attendance date per R1.
    punch_out > punch_in and within 24h. A request that changes nothing -> 422. 404 if employee or record missing.
    Recompute all derived fields, append exactly ONE history entry {at, by, reason, changes}
    where changes holds only fields that actually changed as {from, to}.
    Concurrency: compare-and-set on history length, retry a few times, then 409; never lose history entries.
    """
    if not EMP_CODE_REGEX.match(emp_code):
        raise HTTPException(status_code=422, detail="emp_code must match pattern ^EMP\\d{4,6}$")
    if not DATE_REGEX.match(date):
        raise HTTPException(status_code=422, detail="date must match YYYY-MM-DD format")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid calendar date in path")

    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee '{emp_code}' not found")

    max_retries = 5
    for _ in range(max_retries):
        current_record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})
        if not current_record:
            raise HTTPException(status_code=404, detail=f"Attendance record for '{emp_code}' on '{date}' not found")

        target_status = payload.status if payload.status is not None else current_record.get("status")

        if target_status in ("ABSENT", "LEAVE"):
            if payload.punch_in is not None or payload.punch_out is not None:
                raise HTTPException(
                    status_code=422,
                    detail="Cannot supply punch_in or punch_out when status is ABSENT or LEAVE",
                )
            target_punch_in = None
            target_punch_out = None
        else:
            # Presence status requires punch_in (supplied or stored)
            if payload.punch_in is not None:
                target_punch_in = ms_to_datetime_truncated(payload.punch_in)
            else:
                target_punch_in = current_record.get("punch_in")

            if target_punch_in is None:
                raise HTTPException(
                    status_code=422,
                    detail="Presence status requires a punch_in",
                )

            # punch_in must stay on the record's attendance date per R1
            calc_record_date = get_record_date_for_punch_in(target_punch_in, emp["shift_start"], emp["shift_end"])
            if calc_record_date != date:
                raise HTTPException(
                    status_code=422,
                    detail=f"punch_in maps to record date '{calc_record_date}', which does not match record date '{date}'",
                )

            # punch_out
            if payload.punch_out is not None:
                target_punch_out = ms_to_datetime_truncated(payload.punch_out)
            else:
                target_punch_out = current_record.get("punch_out")

            if target_punch_out is not None:
                diff_sec = (target_punch_out - target_punch_in).total_seconds()
                if diff_sec <= 0:
                    raise HTTPException(status_code=422, detail="punch_out must be strictly greater than punch_in")
                if diff_sec > 86400:
                    raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

        # Recompute all derived fields
        if target_punch_in is not None:
            new_late_minutes = calculate_late_minutes(target_punch_in, date, emp["shift_start"], emp["shift_end"])
        else:
            new_late_minutes = 0

        if target_punch_out is not None and target_punch_in is not None:
            new_overtime_minutes = calculate_overtime_minutes(target_punch_out, date, emp["shift_start"], emp["shift_end"])
            new_work_hours = calculate_work_hours(target_punch_in, target_punch_out)
            new_half_day = calculate_half_day(new_work_hours)
        else:
            new_overtime_minutes = 0
            new_work_hours = None
            new_half_day = False

        # Build changes dict for fields that actually changed
        changes: dict[str, dict[str, Any]] = {}
        if target_status != current_record.get("status"):
            changes["status"] = {"from": current_record.get("status"), "to": target_status}
        if target_punch_in != current_record.get("punch_in"):
            changes["punch_in"] = {"from": current_record.get("punch_in"), "to": target_punch_in}
        if target_punch_out != current_record.get("punch_out"):
            changes["punch_out"] = {"from": current_record.get("punch_out"), "to": target_punch_out}

        # A request that changes nothing -> 422
        if len(changes) == 0:
            raise HTTPException(status_code=422, detail="Request changes nothing")

        now_sec = int(time.time())
        at_dt = datetime.fromtimestamp(now_sec, tz=timezone.utc)
        history_entry = {
            "at": at_dt,
            "by": payload.regularized_by,
            "reason": payload.reason,
            "changes": changes,
        }

        # Compare-and-set on history length
        cur_history = current_record.get("history") or []
        cur_len = len(cur_history)
        if cur_len == 0:
            cas_query = {
                "emp_code": emp_code,
                "date": date,
                "$or": [{"history": {"$size": 0}}, {"history": {"$exists": False}}, {"history": None}],
            }
        else:
            cas_query = {
                "emp_code": emp_code,
                "date": date,
                "history": {"$size": cur_len},
            }

        update_op = {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        }

        res = db.attendance_logs.update_one(cas_query, update_op)
        if res.matched_count == 1:
            updated_doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})
            return serialize_attendance_doc(updated_doc)

    raise HTTPException(status_code=409, detail="Concurrent update conflict on attendance record")


# ==============================================================================
# 6. ANALYTICS ENDPOINTS (Stage 3)
# ==============================================================================

@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthlyAnalyticsResponse)
def get_employee_monthly_analytics(
    emp_code: str,
    month: str = Query(..., description="Month in YYYY-MM format"),
):
    """
    GET /analytics/employees/{emp_code}/monthly?month:
    working_days, present_days (half day 0.5, Mon-Fri only), leave_days, late_count,
    total_late_minutes, total_overtime_minutes, attendance_pct (present/working*100, null if working_days is 0).
    404 unknown employee. Use an aggregation pipeline.
    """
    if not EMP_CODE_REGEX.match(emp_code):
        raise HTTPException(status_code=422, detail="emp_code must match pattern ^EMP\\d{4,6}$")

    start_date_str, end_date_str, start_d, end_d = get_month_bounds(month)

    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(status_code=404, detail=f"Employee '{emp_code}' not found")

    # Working days (R7: Mon-Fri, counted from joined_on for mid-month joiners)
    emp_joined_d = datetime.strptime(emp["joined_on"], "%Y-%m-%d").date()
    effective_start_d = max(emp_joined_d, start_d)
    working_days = count_working_days(effective_start_d, end_d)

    # Run aggregation pipeline on attendance_logs
    pipeline = build_employee_monthly_pipeline(emp_code, start_date_str, end_date_str)
    res = list(db.attendance_logs.aggregate(pipeline, hint="uniq_emp_code_date"))

    if res:
        doc = res[0]
        present_days = round_half_up(doc.get("present_days", 0.0), 2) or 0.0
        leave_days = doc.get("leave_days", 0)
        late_count = doc.get("late_count", 0)
        total_late_minutes = doc.get("total_late_minutes", 0)
        total_overtime_minutes = doc.get("total_overtime_minutes", 0)
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", response_model=List[DepartmentSummaryItem])
def get_departments_summary(
    month: str = Query(..., description="Month in YYYY-MM format"),
    department: Optional[str] = Query(None, description="Optional department filter"),
):
    """
    GET /analytics/departments/summary?month&department?:
    one item per department sorted by department; headcount (R9, includes zero-log employees, excludes future joiners);
    present_days; avg_work_hours (mean over present-status records having work_hours, computed as sum/count across records, null if none);
    late_count; total_late_minutes; leave_count; on_duty_count. Omit departments with headcount 0.
    Start the pipeline from employees and $lookup logs so zero-log employees are not dropped.
    """
    start_date_str, end_date_str, _, _ = get_month_bounds(month)

    pipeline = build_department_summary_pipeline(start_date_str, end_date_str, department)
    hint = "idx_dept_joined_on" if department else "idx_joined_on"
    results = list(db.employees.aggregate(pipeline, hint=hint))

    output = []
    for doc in results:
        headcount = doc.get("headcount", 0)
        if headcount == 0:
            continue

        work_hours_sum = doc.get("total_work_hours", 0.0)
        work_hours_cnt = doc.get("total_work_hours_count", 0)
        if work_hours_cnt > 0:
            avg_work_hours = round_half_up(work_hours_sum / work_hours_cnt, 2)
        else:
            avg_work_hours = None

        present_days = round_half_up(doc.get("present_days", 0.0), 2) or 0.0

        output.append({
            "department": doc["_id"],
            "headcount": headcount,
            "present_days": present_days,
            "avg_work_hours": avg_work_hours,
            "late_count": doc.get("late_count", 0),
            "total_late_minutes": doc.get("total_late_minutes", 0),
            "leave_count": doc.get("leave_count", 0),
            "on_duty_count": doc.get("on_duty_count", 0),
        })

    return output


@app.get("/analytics/leaderboard/late", response_model=List[LateLeaderboardItem])
def get_late_leaderboard(
    month: str = Query(..., description="Month in YYYY-MM format"),
    limit: int = Query(10, ge=1, le=50, description="Max rank limit"),
    department: Optional[str] = Query(None, description="Optional department filter"),
):
    """
    GET /analytics/leaderboard/late?month&limit(1-50, default 10)&department?:
    total late_minutes per employee (> 0 only), employee data from employees (ignore orphan logs),
    standard competition ranking via $setWindowFields $rank (1,2,2,4), apply department filter BEFORE ranking,
    limit applies AFTER ranking (rank <= limit, may return more than limit rows), order by total desc then emp_code asc.
    """
    start_date_str, end_date_str, _, _ = get_month_bounds(month)

    pipeline = build_late_leaderboard_pipeline(start_date_str, end_date_str, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline, hint="idx_date_late_emp"))

    return results


@app.get("/analytics/departments/{department}/trend", response_model=List[DepartmentTrendItem])
def get_department_trend(
    department: str,
    from_date: str = Query(..., alias="from", description="Start date YYYY-MM-DD"),
    to_date: str = Query(..., alias="to", description="End date YYYY-MM-DD"),
):
    """
    GET /analytics/departments/{department}/trend?from&to:
    exactly one row per calendar day in [from,to] including weekends and empty days (use $densify, no Python loops).
    Fields: date, is_working_day (Mon-Fri), headcount (joined_on <= day), present_count (half day 0.5, any weekday/weekend),
    late_count, attendance_rate (present/headcount, 4 dp half-up, only on working days with headcount > 0 else null; working day with no records = 0.0),
    moving_avg_7d (mean of non-null rates over current + up to 6 preceding rows inside the range, null if none, 4 dp half-up, via $setWindowFields).
    404 if the department has no employees; 422 if to < from or range > 92 days.
    """
    if not DATE_REGEX.match(from_date):
        raise HTTPException(status_code=422, detail="'from' must match YYYY-MM-DD format")
    try:
        d_from = datetime.strptime(from_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid calendar date for 'from'")

    if not DATE_REGEX.match(to_date):
        raise HTTPException(status_code=422, detail="'to' must match YYYY-MM-DD format")
    try:
        d_to = datetime.strptime(to_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid calendar date for 'to'")

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' date must be greater than or equal to 'from' date")

    day_count = (d_to - d_from).days + 1
    if day_count > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # 404 if the department has no employees
    emp_count = db.employees.count_documents({"department": department})
    if emp_count == 0:
        raise HTTPException(status_code=404, detail=f"Department '{department}' not found or has no employees")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    results = list(db.employees.aggregate(pipeline, hint="idx_dept_emp_code"))

    output = []
    for item in results:
        present_count = round_half_up(item.get("present_count", 0.0), 2) or 0.0
        attendance_rate = round_half_up(item.get("attendance_rate"), 4)
        moving_avg_7d = round_half_up(item.get("moving_avg_7d"), 4)

        output.append({
            "date": item["date"],
            "is_working_day": item["is_working_day"],
            "headcount": item["headcount"],
            "present_count": present_count,
            "late_count": item["late_count"],
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving_avg_7d,
        })

    return output


# ==============================================================================
# 7. EXPLAIN ENDPOINT (Stage 4)
# ==============================================================================

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = Query(None, description="Employee code"),
    month: Optional[str] = Query(None, description="Month in YYYY-MM format"),
    department: Optional[str] = Query(None, description="Department name"),
    date_from: Optional[str] = Query(None, description="Start date YYYY-MM-DD for attendance list"),
    date_to: Optional[str] = Query(None, description="End date YYYY-MM-DD for attendance list"),
    status_filter: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = Query(None, alias="status", description="Status filter"),
    limit: Optional[int] = Query(10, ge=1, le=50, description="Rank limit for late leaderboard"),
    from_date: Optional[str] = Query(None, alias="from", description="Start date YYYY-MM-DD for trend"),
    to_date: Optional[str] = Query(None, alias="to", description="End date YYYY-MM-DD for trend"),
):
    """
    GET /admin/explain/{endpoint}:
    endpoint in attendance_list, employee_monthly, department_summary, late_leaderboard, department_trend.
    Same query params as the real endpoint; missing required -> 422.
    Build the query/pipeline with the SAME builder functions the real endpoint uses,
    run explain with executionStats, return {"endpoint","collection","explain"} (JSON-safe via bson.json_util).
    Plans must show IXSCAN and no COLLSCAN on ~100k records (use hint if needed).
    """
    if endpoint == "attendance_list":
        if emp_code is not None and not EMP_CODE_REGEX.match(emp_code):
            raise HTTPException(status_code=422, detail="emp_code must match pattern ^EMP\\d{4,6}$")
        if date_from is not None:
            if not DATE_REGEX.match(date_from):
                raise HTTPException(status_code=422, detail="date_from must match YYYY-MM-DD format")
            try:
                datetime.strptime(date_from, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid calendar date for date_from")
        if date_to is not None:
            if not DATE_REGEX.match(date_to):
                raise HTTPException(status_code=422, detail="date_to must match YYYY-MM-DD format")
            try:
                datetime.strptime(date_to, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid calendar date for date_to")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be less than or equal to date_to")

        collection = "attendance_logs"
        query = build_attendance_list_query(emp_code, date_from, date_to, status_filter)
        cmd = {
            "explain": {
                "find": collection,
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "hint": "idx_date_desc_emp_code",
            },
            "verbosity": "executionStats",
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="Missing required query parameter: emp_code")
        if not month:
            raise HTTPException(status_code=422, detail="Missing required query parameter: month")
        if not EMP_CODE_REGEX.match(emp_code):
            raise HTTPException(status_code=422, detail="emp_code must match pattern ^EMP\\d{4,6}$")

        start_date_str, end_date_str, _, _ = get_month_bounds(month)
        collection = "attendance_logs"
        pipeline = build_employee_monthly_pipeline(emp_code, start_date_str, end_date_str)
        cmd = {
            "explain": {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
                "hint": "uniq_emp_code_date",
            },
            "verbosity": "executionStats",
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="Missing required query parameter: month")

        start_date_str, end_date_str, _, _ = get_month_bounds(month)
        collection = "employees"
        pipeline = build_department_summary_pipeline(start_date_str, end_date_str, department)
        hint = "idx_dept_joined_on" if department else "idx_joined_on"
        cmd = {
            "explain": {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
                "hint": hint,
            },
            "verbosity": "executionStats",
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="Missing required query parameter: month")

        start_date_str, end_date_str, _, _ = get_month_bounds(month)
        collection = "attendance_logs"
        pipeline = build_late_leaderboard_pipeline(start_date_str, end_date_str, limit or 10, department)
        cmd = {
            "explain": {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
                "hint": "idx_date_late_emp",
            },
            "verbosity": "executionStats",
        }

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="Missing required query parameter: department")
        if not from_date:
            raise HTTPException(status_code=422, detail="Missing required query parameter: from")
        if not to_date:
            raise HTTPException(status_code=422, detail="Missing required query parameter: to")

        if not DATE_REGEX.match(from_date):
            raise HTTPException(status_code=422, detail="'from' must match YYYY-MM-DD format")
        try:
            d_from = datetime.strptime(from_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid calendar date for 'from'")

        if not DATE_REGEX.match(to_date):
            raise HTTPException(status_code=422, detail="'to' must match YYYY-MM-DD format")
        try:
            d_to = datetime.strptime(to_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid calendar date for 'to'")

        if d_to < d_from:
            raise HTTPException(status_code=422, detail="'to' date must be greater than or equal to 'from' date")

        day_count = (d_to - d_from).days + 1
        if day_count > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        collection = "employees"
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        cmd = {
            "explain": {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
                "hint": "idx_dept_emp_code",
            },
            "verbosity": "executionStats",
        }

    try:
        raw_explain = db.command(cmd)
    except PyMongoError as exc:
        logger.error(f"Error running explain for {endpoint}: {exc}")
        raise HTTPException(status_code=503, detail="Database error during explain")

    explain_safe = json.loads(json_util.dumps(raw_explain))

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain_safe,
    }



