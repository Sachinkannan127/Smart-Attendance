# Architectural & Design Decisions

### 1. Concurrency Control
We avoid application-level check-then-insert races by offloading concurrency guards to MongoDB. Punch-in relies on a composite unique index on `(emp_code, date)` and catches `DuplicateKeyError` to return 409. Punch-out executes an atomic `update_one` with filter `{"punch_out": None}`, returning 409 if `matched_count == 0`. Attendance regularization uses optimistic concurrency via Compare-and-Set (CAS), checking `history` array length before appending to guarantee audit log integrity under concurrent edits.

### 2. Timezone & Overnight Shifts
All dates and shift boundaries are standardized to Indian Standard Time (`UTC+05:30`). For overnight shifts where `shift_end <= shift_start` (e.g., 22:00 to 06:00), punch-ins before `shift_end` are anchored to the previous calendar day's shift window. Instants are stored as normalized UTC epoch milliseconds.

### 3. Aggregation Pipelines vs. Python
Heavy analytical processing—including department rollups with `$lookup`, gapless timeline creation with `$densify`, and competition ranking with `$rank`—is executed entirely in MongoDB to prevent transferring large, unindexed datasets into memory. Python is reserved for deterministic per-record business rules: 600-second grace evaluation, overtime thresholds, and `Decimal ROUND_HALF_UP` precision.

### 4. Index Considered and Rejected
We considered creating an index on `{punch_in: 1, punch_out: 1}` on `attendance_logs`. We rejected it because queries and sorts consistently filter by `date` and sort by `(date, emp_code)`. A compound index on `(date: -1, emp_code: 1)` fully covers listing, pagination, and range scans with zero in-memory sort, making the punch timestamp index redundant overhead.

### 5. What I’d Change for 100x Data
At 100x scale (~10M logs), we would:
1. Replace `skip/limit` pagination with keyset pagination on `(date, emp_code)`.
2. Partition/shard `attendance_logs` by `date` range or hashed `emp_code`.
3. Pre-aggregate daily and monthly metrics into dedicated summary collections on punch-out/regularization rather than running real-time full-month `$lookup` aggregations.
