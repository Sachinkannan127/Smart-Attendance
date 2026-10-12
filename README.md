# Smart-Attendance API

Employee Attendance & Analytics API built with Python 3.11+, FastAPI, PyMongo (sync, `tz_aware=True`), and MongoDB 6+/7.

## Key Features

- **Strict Validation & Boundary Conversion**:
  - Employee codes validated via strict pattern `^EMP\d{4,6}$`.
  - Instants across all endpoints handled in integer epoch milliseconds (`StrictEpochMs`), truncated to whole seconds.
  - Strict calendar dates (`YYYY-MM-DD`) and months (`YYYY-MM`).
  - MongoDB `_id` is never accepted or returned in any API request or response.
- **Business Rules (R1 - R5)**:
  - **R1 (IST & Shifts)**: All attendance calculated in Indian Standard Time (`UTC+05:30`). Handles day shifts and overnight shifts (`shift_end <= shift_start`).
  - **R2 (Late Arrival)**: Strict 10-minute grace period. If punch-in is strictly $> 600$ seconds after shift start, late minutes are counted from shift start.
  - **R3 (Overtime)**: Overtime minutes counted from shift end if $\ge 30$ minutes, else 0.
  - **R4 (Work Hours)**: Computed to 2 decimal places using `ROUND_HALF_UP` (no banker's rounding).
  - **R5 (Half Day)**: Work hours $< 4.50$ counts as half day (0.5 present day).
- **Concurrency & History**:
  - Unique indexes on `emp_code` and `(emp_code, date)` prevent duplicate records.
  - Atomic compare-and-set (CAS) on `punch_out` and regularization history length ensures zero loss of regularization audit history.
- **Analytics & Pipelines**:
  - Monthly employee attendance with mid-month joiner handling.
  - Department summaries with headcount, average work hours, and zero-log retention.
  - Late leaderboard with standard competition ranking ($rank 1, 2, 2, 4) using `$setWindowFields`.
  - Department trends using MongoDB `$densify` for gapless calendar day reporting and 7-day moving averages.
- **Admin Query Explain**:
  - Inspects query plans with `executionStats` to verify `IXSCAN` index usage.

## Setup & Running

### Prerequisites
- Python 3.11+
- MongoDB 6.0+ running locally or via `MONGO_URI`

### Installation
```bash
pip install -r requirements.txt
```

### Environment Variables
Configure environment variables or a `.env` file (real environment variables always take precedence):
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### Starting the Server
```bash
uvicorn app.main:app --port 8000
```
