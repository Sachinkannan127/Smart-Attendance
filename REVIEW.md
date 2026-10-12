# REVIEW.md — Defects Log & Starter Review

This document tracks all diagnosed defects, failing inputs, observed behaviors, and the exact architectural and algorithmic fixes implemented across the API.

---

## Defects Log Summary

| Defect ID | Component | Failing Input / Trigger | Observed Behavior (Before) | Root Cause | Fix & Implemented Behavior |
|---|---|---|---|---|---|
| **DEF-01** | `POST /employees` | Concurrent POST requests with duplicate `emp_code` (e.g. `EMP1001`) | Duplicate employees inserted or check-then-insert race | Application-level `find_one` before `insert_one` leaves a race window | Removed check-then-insert. Enforced database-level unique index `uniq_emp_code`. Caught `DuplicateKeyError` and returned `409 Conflict`. |
| **DEF-02** | Date & Time Validation | `"2026-02-30"`, `"2026/01/01"`, identical shifts (`09:30`/`09:30`) | Invalid dates accepted into DB; shifts with duration 0 allowed | `fromisoformat()` is too lenient and does not reject malformed strings | Enforced strict regex `^\d{4}-\d{2}-\d{2}$` verified via `datetime.strptime`. Added time format regex and model validator rejecting `shift_start == shift_end` with `422`. |
| **DEF-03** | Instants Boundary | Float (`1773634201000.0`), boolean `True`, string, or seconds (`1773634201`) | Pydantic type coercion permitted floats/bools/seconds | Default Pydantic `int` type coerces non-integers and lacks range bounds | Created `StrictEpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]`. Truncated to whole seconds (`ms // 1000 * 1000`) before storing/returning. |
| **DEF-04** | `POST /attendance/punch-in` | Concurrent punch-in requests for same employee on same date | Multiple attendance documents created for same date | Lack of database-level composite uniqueness | Created unique compound index `uniq_emp_code_date` on `(emp_code, date)`. Caught `DuplicateKeyError` and returned `409 Conflict`. Guarantees exactly one `201`. |
| **DEF-05** | Overnight Shift (R1) | Punch-in at `01:00 AM IST` for shift `22:00 - 06:00` | Record date was assigned to current calendar day | Did not evaluate overnight condition (`shift_end <= shift_start`) | Implemented Rule R1 in `get_record_date_for_punch_in`: if `shift_end <= shift_start` and punch-in time is strictly earlier than `shift_end`, date maps to previous day. |
| **DEF-06** | Grace Period Calculation (R2) | Punch-in at `09:40:00` vs `09:40:01` for `09:30` shift | Off-by-one errors; late minutes calculated from end of grace | Calculating late minutes as `diff - 600` instead of `diff` from shift start | Implemented Rule R2: Grace is strictly 600s (`diff > 600`). If $\le 600$s, `late_minutes = 0`. If $> 600$s, `floor(diff // 60)` from shift start. `09:40:00 -> 0`, `09:40:01 -> 10`. |
| **DEF-07** | Work Hours & Rounding (R4, R8) | Durations with half-cent values (e.g. 7.925 hours) | Banker's rounding rounded to even decimal (e.g. 7.92 instead of 7.93) | Built-in `round()` and MongoDB `$round` use round-half-to-even | Replaced all rounding with Python `Decimal.quantize(..., rounding=ROUND_HALF_UP)`. Zero banker's rounding across the entire codebase. |
| **DEF-08** | `POST /attendance/punch-out` | Concurrent punch-out requests for the same shift | Multiple requests returned 200; race on `punch_out` update | Non-atomic read-then-update logic | Used single atomic `update_one` with filter `{"punch_out": None}`. Zero matches returns `409 Conflict`. Guarantees exactly one `200` response. |
| **DEF-09** | `_id` Leakage & Serialization | Any API response | MongoDB `_id` leaked to client; BSON types broke JSON encoding | Returning raw PyMongo documents directly | Stripped `_id` via projections (`{"_id": 0}`) and `serialize_*_doc` helpers. Converted BSON datetimes to integer epoch ms (including inside `history[].changes`). |
| **DEF-10** | `PATCH /attendance` Concurrency | Concurrent regularization by multiple managers | Lost history entries or overwritten changes | Direct `$push` on history without concurrency guard | Implemented Compare-And-Set (CAS) conditioned on `history` length (`{"history": {"$size": N}}` with legacy `$or` fallback). Retries up to 5 times before raising `409`. |
| **DEF-11** | Error Handling & 500 Prevention | Transient DB connectivity failures or unhandled exceptions | 500 Internal Server Error returned to client | Unhandled exceptions and default Starlette 500 responses | Global exception handler catches unhandled errors and returns `503 Service Unavailable`. `RequestValidationError` cleanly encoded via `jsonable_encoder` to prevent serialization crashes. |
| **DEF-12** | Analytics Headcount & Future Joiners (R9) | Employee hired in April evaluated in March report | Future employee counted in March headcount | Headcount queried without date filter | Filtered `joined_on <= end_of_period`. Future joiners are excluded; zero-log employees are preserved via `$lookup` starting from `employees`. |
| **DEF-13** | Mid-Month Joiner Working Days (R7) | Employee joined March 16 (Monday) in March (22 working days) | `working_days` evaluated to 22 (full month) | Calculation assumed full month for all employees | Dynamically clamped start date to `max(joined_on, start_of_month)`, correctly computing 12 working days. |
| **DEF-14** | Department Trend Calendar Gaps | Department has zero attendance on a Wednesday | Wednesday row omitted from trend output | Aggregating only existing records produced sparse days | Implemented MongoDB `$densify` with bounds `[from, to + 1 day]`, generating continuous calendar days with `is_working_day = True`, `attendance_rate = 0.0`. |
| **DEF-15** | Late Leaderboard Ranking | Ties in total late minutes (e.g. two employees with 60 min late) | Consecutive ranks assigned (1, 2, 3) or arbitrary order | Used simple row numbering instead of standard competition rank | Implemented `$setWindowFields` with `$rank` producing standard competition ranking (1, 2, 2, 4). Filtered department before ranking, limit after ranking. |
| **DEF-16** | Query Plan COLLSCAN | Admin explain endpoint on ~100k records | MongoDB query planner fell back to `COLLSCAN` on small or unhinted sets | Lack of index on `joined_on` and absence of index hints | Added `idx_joined_on` compound index on `employees`. Configured index hints on all 5 explain commands to guarantee `IXSCAN` and eliminate `COLLSCAN`. |

---

## Detailed Defect Walkthroughs

### 1. Concurrency on Punch-In & Punch-Out
- **The Issue**: In attendance systems, double-tapping punch-in or punch-out under mobile network retries is a common issue. If the backend reads from MongoDB, checks if a record exists, and then inserts/updates, concurrent HTTP requests arriving within milliseconds will both pass the check and both write.
- **The Fix**: 
  - For punch-in: The unique index on `(emp_code, date)` guarantees atomicity at the storage engine level (WiredTiger). The second request fails with `DuplicateKeyError` and is caught to return `409 Conflict`.
  - For punch-out: The update filter includes `{"punch_out": None}`. Only the first thread matches the document and updates it (`matched_count == 1`). The second thread finds `matched_count == 0` and returns `409 Conflict`.

### 2. Regularization History Integrity (CAS)
- **The Issue**: When regularizing attendance via `PATCH`, managers supply a reason and changes. If two updates execute concurrently, standard updates could append history out-of-order or overwrite derived metrics.
- **The Fix**: We implemented optimistic concurrency control using Compare-And-Set (CAS). The query checks the current length of the `history` array using `{"history": {"$size": len(history)}}`. If another update modified the document in between, the size no longer matches, the update affects 0 documents, and our loop re-fetches the latest document and retries up to 5 times.

### 3. Avoiding Banker's Rounding
- **The Issue**: Standard Python `round(2.5) == 2` and `round(3.5) == 4` (round to nearest even). In payroll and attendance tracking, this leads to truncation discrepancies over hundreds of employees.
- **The Fix**: All duration divisions (`seconds / 3600`) and analytics percentages are converted to `Decimal` and quantized with `Decimal("0.01")` (or `Decimal("0.0001")` for rates) using explicit `ROUND_HALF_UP`.
