# REVIEW.md — Defects Log & Starter Review

This document records the defects identified in the starter codebase, the failing inputs that expose them, their root causes, and the fixes applied.

---

## 1. Starter Defects Log

| Defect ID | Function / Component | Failing Input / Trigger | Starter Observed Behavior | Root Cause | Fix & Implemented Behavior |
|---|---|---|---|---|---|
| **DEF-01** | `list_employees` (`GET /employees`) | `page=1, page_size=10`, `department="Engineering"` | Skips first 10 employees on page 1; `total` reports count of all employees across all departments. | Uses `skip(page * page_size)` (0-based math on 1-based page); uses `count_documents({})` without filter. | Changed skip to `(page - 1) * page_size`; filtered count with `count_documents(filter_query)`. |
| **DEF-02** | `punch_in` (`POST /attendance/punch-in`) | `{"emp_code": "EMP9999", ...}` where employee does not exist | Returns `500 Internal Server Error`. | Dereferenced `employee["shift_start"]` without verifying if `find_one` returned `None`. | Explicit check: raises `404 Not Found` if employee record does not exist. |
| **DEF-03** | `list_attendance` (`GET /attendance`) | Querying large attendance log collections | High latency and excessive memory usage. | Loaded all documents into Python via `list(collection.find())` and sorted in Python memory using `sorted()`. | Replaced with database-level compound index sorting `[("date", -1), ("emp_code", 1)]`, `.skip()`, and `.limit()`. |
| **DEF-04** | `health_check` (`GET /health`) | MongoDB service stopped or unreachable | Returns `200 OK` with `{"status": "ok"}`. | Static JSON dictionary returned without pinging or checking database connectivity. | Issues `db.command("ping")` within 2-second timeout; returns `503 Service Unavailable` if database is down. |
| **DEF-05** | Date & Shift Timezone Handling | Punches near midnight or running on UTC server | Shifts assigned to incorrect dates; overnight shifts misaligned. | Used naive `datetime.now()` and local server time instead of Indian Standard Time (`UTC+05:30`). | Standardized all calendar dates and shift boundaries to `IST` (`UTC+05:30`), with explicit overnight date anchoring. |
| **DEF-06** | Timestamp Falsy Evaluation | `punched_at = 0` (or epoch 0) | Silently overrides input and defaults to current system time. | Checked presence using `if not punched_at:` which treats `0` as falsy. | Explicit check `if punched_at is None:` and enforced strict epoch bounds (`StrictEpochMs`), rejecting invalid timestamps. |
| **DEF-07** | Punch-In Status Validation | `{"emp_code": "EMP1001", "status": "ABSENT"}` | Accepts `ABSENT` and `LEAVE` on punch-in. | Permitted all daily attendance statuses instead of restricting punch-in to active working presence. | Restricted punch-in status enum strictly to `PRESENT`, `WFH`, and `ON_DUTY`; rejects others with `422`. |
| **DEF-08** | Concurrent Punch-In Race Condition | Concurrent POST requests for same employee and date | Duplicate attendance documents created for the same date. | Application-level check-then-insert (`find_one` then `insert_one`) without database constraint. | Created unique compound index on `(emp_code, date)`; caught `DuplicateKeyError` and returned `409 Conflict`. |

---

## 2. Examined and Decided Fine

The following aspects of the starter codebase were reviewed and retained without architectural changes:

1. **Synchronous PyMongo with FastAPI Worker Pool**: Defining route functions with standard `def` allows FastAPI to execute database calls in the AnyIO worker thread pool. PyMongo's thread-safe connection pool handles concurrent requests cleanly without blocking the event loop.
2. **Lifespan Context Manager**: The `@asynccontextmanager` startup pattern is appropriate for idempotent database connection checks and initial index creation.
3. **Pydantic Validation**: Using Pydantic models for incoming JSON payloads provides consistent structural schema enforcement and clear error reporting.
