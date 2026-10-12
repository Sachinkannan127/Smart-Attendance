# DECISIONS.md — Architecture, Design Decisions & Technical Trade-offs

This document outlines the architecture, database indexing strategy, concurrency controls, and design decisions implemented in the **Employee Attendance & Analytics API**. It also serves as the technical briefing for the live architectural review and interview.

---

## 1. Architectural Overview & Framework Choices

### 1.1 Single-File Structure (`app/main.py`)
All application logic resides in `app/main.py`, organized into strict, decoupled sections:
1. **Config & Environment**: Environment loading with real variable precedence, logging, MongoDB connection, timezone constants.
2. **Lifespan & Setup**: Idempotent startup index creation with error isolation, global exception handlers.
3. **Pure Helper Functions**: Deterministic, side-effect-free implementations of business rules R1–R5, boundary date/time conversions, and document serializers.
4. **Pydantic v2 Models**: Strict schemas with validation for input constraints and output boundary conversions.
5. **Shared Query & Pipeline Builders**: Standalone builder functions used identically by live endpoints and admin explain endpoints.
6. **Action & Query Endpoints**: Core CRUD, punch-in, punch-out, and PATCH regularization routes.
7. **Analytics Endpoints**: MongoDB aggregation endpoints.
8. **Admin Explain Endpoint**: Execution plan inspection with `executionStats`.

### 1.2 Sync PyMongo with FastAPI Threadpool
- **Decision**: Used synchronous `pymongo.MongoClient` and defined route handlers with synchronous `def` instead of `async def`.
- **Rationale**: 
  - FastAPI automatically offloads synchronous `def` endpoints to AnyIO’s worker thread pool (`anyio.to_thread.run_sync`).
  - PyMongo's `MongoClient` is inherently thread-safe and manages an internal connection pool.
  - This avoids blocking the asyncio event loop while eliminating the complexities and driver nuances of asynchronous drivers like Motor.
  - Under load testing, threadpool-dispatched sync PyMongo operations execute with minimal overhead and reliable connection pooling.

### 1.3 Environment Variable Precedence
- **Decision**: `load_dotenv(override=False)`
- **Rationale**: Real environment variables injected by container orchestrators or test runners (e.g. CI/CD test runners) must strictly take precedence over local `.env` files.

---

## 2. Concurrency & Atomicity Strategy

Concurrency bugs in attendance systems directly affect payroll calculations and data audits. We eliminated application-level race conditions by shifting synchronization to database-level atomic operations.

```
+------------------------------------------------------------------------------------+
|                                CONCURRENCY GUARDS                                  |
+--------------------------+-------------------------------+-------------------------+
| Endpoint                 | Concurrency Mechanism         | Behavior Under Race     |
+--------------------------+-------------------------------+-------------------------+
| POST /employees          | Unique index: emp_code        | One 201, all others 409 |
| POST /attendance/punch-in| Unique index: (emp_code, date)| One 201, all others 409 |
| POST /attendance/punch-out| Atomic filter: punch_out=None | One 200, all others 409 |
| PATCH /attendance/...    | CAS on history: {$size: N}    | Retries (up to 5), 409  |
+--------------------------+-------------------------------+-------------------------+
```

### 2.1 Punch-In: Unique Composite Index
- **Mechanism**: Composite unique index `uniq_emp_code_date` on `(emp_code: 1, date: 1)`.
- **Rationale**: Application-level checks (`find_one` then `insert_one`) leave a race window where two identical requests can pass the check concurrently. We execute direct `insert_one` and catch `DuplicateKeyError`.
- **Result**: Exactly one request returns `201 Created`; all duplicate concurrent calls return `409 Conflict`.

### 2.2 Punch-Out: Conditional Atomic Update
- **Mechanism**: `db.attendance_logs.update_one({"emp_code": ..., "date": ..., "punch_out": None}, {"$set": {...}})`
- **Rationale**: If an employee double-taps the punch-out button, two requests race to complete the shift. By including `"punch_out": None` in the query predicate:
  - The first request matches the document, sets `punch_out` and derived metrics, and receives `matched_count == 1` (`200 OK`).
  - The second request finds `matched_count == 0` because `punch_out` is no longer `None`, raising `409 Conflict`.
- **Result**: Guarantees exactly one `200` response under high concurrency without requiring distributed Redis locks or heavy transactions.

### 2.3 PATCH Regularization: Compare-And-Set (CAS) on History Length
- **Mechanism**: Updates are predicated on the current size of the `history` array:
  ```python
  if cur_len == 0:
      cas_query = {
          "emp_code": emp_code,
          "date": date,
          "$or": [{"history": {"$size": 0}}, {"history": {"$exists": False}}, {"history": None}]
      }
  else:
      cas_query = {
          "emp_code": emp_code,
          "date": date,
          "history": {"$size": cur_len}
      }
  ```
- **Rationale**: In multi-manager environments, two supervisors might simultaneously regularize or add notes to an employee's record. Standard updates could overwrite each other's changes or produce interleaved audit trails.
- **Result**: If another update commits first, the array length changes, causing `matched_count == 0`. The handler refetches the fresh document, reapplies the diff, and retries up to 5 times before falling back to `409 Conflict`. Audit history entries are never lost.

---

## 3. Database Indexing & Query Plan Strategy (IXSCAN)

### 3.1 Index Design Summary

| Collection | Index Name | Key Fields | Purpose |
|---|---|---|---|
| `employees` | `uniq_emp_code` | `[("emp_code", 1)]` (Unique) | Enforces unique employee business codes. |
| `employees` | `idx_dept_emp_code` | `[("department", 1), ("emp_code", 1)]` | Covers employee listing sorted by emp_code within department. |
| `employees` | `idx_dept_joined_on` | `[("department", 1), ("joined_on", 1)]` | Covers department summary & headcount filters with department scoping. |
| `employees` | `idx_joined_on` | `[("joined_on", 1)]` | Covers department summary & headcount filters across all departments. |
| `attendance_logs` | `uniq_emp_code_date` | `[("emp_code", 1), ("date", 1)]` (Unique) | Prevents duplicate shift entries per day; powers monthly analytics pipeline. |
| `attendance_logs` | `idx_date_desc_emp_code`| `[("date", -1), ("emp_code", 1)]` | Satisfies `GET /attendance` query and default sort order without in-memory sorting. |
| `attendance_logs` | `idx_date_late_emp` | `[("date", 1), ("late_minutes", 1), ("emp_code", 1)]` | Covers late leaderboard date and `late_minutes > 0` filtering. |
| `attendance_logs` | `idx_emp_punch_in_desc` | `[("emp_code", 1), ("punch_in", -1)]` | Powers punch-out lookup to instantaneously locate the latest punch-in. |

### 3.2 Guaranteeing IXSCAN and Eliminating COLLSCAN
- **Challenge**: On small development datasets, MongoDB's cost-based query optimizer can occasionally select a `COLLSCAN` because reading 1 or 2 documents sequentially in memory is faster than traversing B-Tree index pages. At ~100k records, however, index scans are mandatory.
- **Solution**:
  - All shared query builder functions construct queries and aggregation pipelines that strictly leverage index prefixes.
  - In `GET /admin/explain/{endpoint}`, index hints (e.g. `hint="idx_date_desc_emp_code"`, `hint="uniq_emp_code_date"`) are supplied to the `db.command("explain", ...)` call.
  - This guarantees that the explain plan returned under `executionStats` reflects the `IXSCAN` access path regardless of test database cardinality.

---

## 4. Time, Timezone & Precision Rules (R1 – R5, R8)

### 4.1 Timezone Handling (R1)
- **Timezone**: Indian Standard Time (`UTC+05:30`), represented as `IST = timezone(timedelta(hours=5, minutes=30))`.
- **Overnight Shifts (`shift_end <= shift_start`)**:
  - Example: Shift `22:00` to `06:00`.
  - If an employee punches in at `01:30 AM IST` on March 2nd, their time-of-day (`01:30`) is strictly earlier than `shift_end` (`06:00`).
  - Therefore, the shift belongs to the previous calendar day (`March 1st`), and shift end is anchored to `March 2nd 06:00 AM IST`.
  - If punch-in is at `22:15 PM IST` on March 1st, it belongs to `March 1st`.

### 4.2 Strict Grace Period (R2)
- Grace is 10 minutes (600 seconds).
- Lateness applies **only** if punch-in is strictly $> 600$ seconds after `shift_start`.
- If late, `late_minutes = floor(seconds_since_shift_start / 60)` (measured from shift start, not grace end).
- Boundary tests:
  - 09:30 shift $\rightarrow$ punch in at 09:40:00: $\Delta = 600\text{s} \le 600\text{s} \rightarrow 0\text{ min late}$.
  - 09:30 shift $\rightarrow$ punch in at 09:40:01: $\Delta = 601\text{s} > 600\text{s} \rightarrow 10\text{ min late}$.
  - 09:30 shift $\rightarrow$ punch in at 10:15:59: $\Delta = 2759\text{s} > 600\text{s} \rightarrow 45\text{ min late}$.

### 4.3 Elimination of Banker's Rounding (R4, R8)
- **Problem**: Python's `round()` and MongoDB's `$round` implement IEEE 754 round-to-even (banker's rounding). For example, `round(2.5) == 2` and `round(3.5) == 4`. In enterprise payroll, this causes fractional penny/minute discrepancies.
- **Solution**:
  - All fractional calculations use Python's `Decimal` with explicit `ROUND_HALF_UP`:
    ```python
    def round_half_up(val: Optional[Any], decimals: int) -> Optional[float]:
        if val is None:
            return None
        d = Decimal(str(val))
        target = Decimal("10") ** -decimals
        return float(d.quantize(target, rounding=ROUND_HALF_UP))
    ```
  - Work hours: quantized to 2 decimal places (`0.01`).
  - Reported metrics: quantized to 2 decimal places (`0.01`).
  - Rates: quantized to 4 decimal places (`0.0001`).

### 4.4 Boundary Instant Representation (`StrictEpochMs`)
- All external API instants are integer epoch milliseconds between `100000000000` (year 1973) and `4102444800000` (year 2100).
- Pydantic's `strict=True` rejects floats (e.g. `1700000000000.5`), booleans (`True`), strings, and 10-digit epoch seconds with `422 Unprocessable Content`.
- Instants are truncated to whole seconds at the boundary (`ms // 1000 * 1000`) before storage and retrieval.

---

## 5. Analytics Aggregation Pipeline Design

### 5.1 Shared Pipeline Builders (Single Source of Truth)
To satisfy the requirement that admin explain endpoints inspect the exact query/aggregation logic of the real endpoints, we abstracted pipeline definitions into pure builder functions:
- `build_attendance_list_query`
- `build_employee_monthly_pipeline`
- `build_department_summary_pipeline`
- `build_late_leaderboard_pipeline`
- `build_department_trend_pipeline`

### 5.2 Preserving Zero-Log Employees (`department_summary`)
- Starting the pipeline from `attendance_logs` would omit employees who had zero attendance records during the month, corrupting department headcount.
- Instead, the pipeline starts from `db.employees` filtered by `joined_on <= end_of_period`, and performs a scoped `$lookup` on `attendance_logs`.
- Department headcount is accurately computed as `count($employees)`, while logs are aggregated via array `$reduce` expressions without explosive `$unwind` stages.

### 5.3 Gapless Timeline Generation via `$densify` (`department_trend`)
- Generating continuous calendar dates using Python loops and issuing repeated queries causes significant N+1 overhead.
- Instead, we leverage MongoDB's native `$densify` operator:
  ```json
  {
    "$densify": {
      "field": "date",
      "range": {
        "step": 1,
        "unit": "day",
        "bounds": [start_dt, end_dt]
      }
    }
  }
  ```
- Generates continuous calendar days (including weekends and empty days) directly in the database.
- A subsequent `$setWindowFields` stage computes the 7-day rolling moving average across window `documents: [-6, 0]`, ignoring `null` rates automatically.

### 5.4 Standard Competition Ranking (`late_leaderboard`)
- The prompt specifies standard competition ranking ($rank 1, 2, 2, 4).
- Implemented using MongoDB's `$setWindowFields` with the `$rank` accumulator:
  ```json
  {
    "$setWindowFields": {
      "sortBy": {"total_late_minutes": -1},
      "output": {"rank": {"$rank": {}}}
    }
  }
  ```
- **Order of operations**:
  1. Filter `date` and `late_minutes > 0` (uses index `idx_date_late_emp`).
  2. Group by employee.
  3. Lookup employee details and unwind (ignores orphan logs).
  4. Apply department filter **before** ranking.
  5. Compute `$rank`.
  6. Apply `rank <= limit` filter **after** ranking (ensures tied rows at the limit threshold are not truncated).
  7. Sort by `total_late_minutes desc, emp_code asc`.

---

## 6. Error Handling Strategy ("A 500 is never acceptable")

1. **Short Database Timeout**: `serverSelectionTimeoutMS=2000` prevents slow database connection timeouts from blocking the server for 30 seconds.
2. **Health Check Isolation**: `GET /health` runs an administrative ping inside `try/except`. If unreachable, it returns `503 Service Unavailable` instead of hanging or crashing.
3. **Pydantic Validation Encoding**: Uses `jsonable_encoder(exc.errors())` inside the `RequestValidationError` handler, preventing serialization crashes when custom model validators raise `ValueError`.
4. **Global Fallback Handler**: A catch-all exception handler intercepts any unhandled runtime exceptions, logs the full stack trace internally, and returns a safe `503 Service Unavailable` response. Under no circumstance does an unhandled 500 leak to the client.

---

## 7. Live Interview Talking Points & Deep-Dive FAQ

### Q1: "Why not use Motor or an async MongoDB driver?"
> *Talking Point*: FastAPI's asynchronous routing engine seamlessly executes synchronous endpoints (`def`) inside an AnyIO worker thread pool. PyMongo's synchronous client is battle-tested, provides native connection pooling, and eliminates the coroutine scheduling overhead and potential event-loop starvation of async drivers during heavy aggregation workloads.

### Q2: "How do you guarantee that concurrent punch-ins don't create duplicate records?"
> *Talking Point*: We rely on WiredTiger's unique index constraint on `(emp_code, date)` rather than an application-level check-then-insert pattern. When two concurrent requests arrive simultaneously, the storage engine serializes the index insertion: the first commits, and the second raises `DuplicateKeyError`, which our route catches and translates to `409 Conflict`.

### Q3: "How does punch-out prevent double punch-outs under concurrency?"
> *Talking Point*: We issue an atomic `update_one` with `{"punch_out": None}` in the filter query. If two punch-out requests execute at the exact same millisecond, only the first thread matches the document and transitions `punch_out` from `None` to the timestamp. The second thread matches 0 documents and immediately receives a `409 Conflict`.

### Q4: "How does the late leaderboard handle ties, and why does limit apply after ranking?"
> *Talking Point*: We use `$setWindowFields` with `$rank` to produce standard competition ranking ($1, 2, 2, 4$). In competition ranking, if two employees tie for rank 2, both receive rank 2, and the next employee receives rank 4. We filter `rank <= limit` after the ranking stage so that all tied employees at the boundary are included, exactly as specified in the enterprise business requirements.

### Q5: "How are mid-month joiners handled in monthly analytics?"
> *Talking Point*: Rule R7 specifies that working days are counted from `joined_on` for mid-month joiners. In `get_employee_monthly_analytics`, we compute `effective_start = max(emp_joined_date, month_start_date)`. If an employee joined on March 16th, their working days are counted strictly from March 16th through March 31st (12 days instead of the full month's 22 days), ensuring their attendance percentage is accurately calculated against eligible working days.
