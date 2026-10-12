# DECISIONS.md

1. **Indexes.** `attendance_logs`: unique `(emp_code, date)` (one record per day; serves per-employee queries and lookups), `(date desc, emp_code)` (the list sort and date-range analytics), `(emp_code, punch_in desc)` (finds the open record at punch-out). `employees`: unique `emp_code`, plus `(department, emp_code)`, `(department, joined_on)` and `joined_on`. Rejected: an index on `status`; five values filter almost nothing and cost writes.
2. **Punch-in race.** Both requests build the same document and call `insert_one`. The unique `(emp_code, date)` index lets exactly one succeed; the other gets `DuplicateKeyError`, which I return as 409. No check-then-insert.
3. **Ties.** The leaderboard uses `$rank` and keeps `rank <= limit`, so everyone tied at the cutoff is returned. It can return more than `limit` rows.
4. **Headcount.** The summary starts from `employees` (joined by month end) and `$lookup`s each person's logs. Someone with no logs gets an empty result but still counts once.
5. **100x data.** Keep a pre-aggregated monthly summary per employee, updated on each punch or correction, and shard logs by `emp_code`.
