# DECISIONS.md

1. **Indexes.**
   - `employees`: Unique index on `emp_code` guarantees business key uniqueness and race-safety during creation. Compound index on `(department, joined_on)` optimizes department headcount lookups and trend aggregations.
   - `attendance_logs`: Compound unique index on `(emp_code, date)` enforces at most one record per attendance day and handles concurrent punch-in races. Compound index `(date DESC, emp_code ASC, status ASC)` supports fast pagination without in-memory sorting.
   - *Rejected:* Considered indexing `late_minutes` alone, but compound indexing with date prefix served leaderboard filters much better while avoiding index bloat.

2. **Punch-in race.**
   Two concurrent punch-in requests calculate the same IST attendance date. Both attempt an atomic `insert_one`. The database unique index on `(emp_code, date)` allows exactly one insert to commit; the second immediately raises PyMongo's `DuplicateKeyError`, which our exception handler catches and maps to HTTP 409 Conflict.

3. **Ties.**
   The leaderboard uses MongoDB `$rank` inside `$setWindowFields` (standard competition ranking: 1, 2, 2, 4). The `limit` filter (`$match: {rank: {$lte: limit}}`) is applied after ranking. If two employees tie at the cutoff rank, both are returned, causing the result to contain more items than `limit`, exactly as required.

4. **Headcount.**
   The department aggregation starts from `employees` filtering by `department` and `joined_on <= month_end`. It performs a `$lookup` into `attendance_logs` and an `$unwind` with `preserveNullAndEmptyArrays: true`. Employees with zero attendance logs are preserved and counted toward headcount.

5. **One thing you would change for 100x data.**
   Pre-aggregate daily and monthly statistics via an event-driven worker or change streams into a materialized summary collection, avoiding real-time multi-stage aggregations across millions of raw punch documents.