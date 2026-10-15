# DECISIONS.md

**1. Which indexes did you create and why?**
Unique `employees.emp_code` and unique `attendance_logs (emp_code, date)` guarantee one row per key. `(date desc, emp_code)` serves the attendance list order and the leaderboard's month scan. `(status, date)` serves status filters. `employees (department, emp_code)` and `joined_on` serve filtered lists and the summary's first stage.

**2. What happens when two identical punch-ins arrive at the same instant?**
Both call `insert_one`. The unique `(emp_code, date)` index lets exactly one succeed; the other raises `DuplicateKeyError`, which I return as 409. There is no read-then-write gap to race through.

**3. What does your leaderboard return for a tie at the cutoff?**
`$rank` gives tied employees the same rank, and `limit` filters on rank. With limit 2 and ranks 1, 2, 2, 4, I return three rows, ordered by minutes descending, then `emp_code`.

**4. How does your department summary count employees with no logs?**
It starts from `employees` (joined on or before month end) and uses `$lookup` into the logs. `$unwind` with `preserveNullAndEmptyArrays` keeps people with no logs, so they add to headcount and contribute zero to the totals.

**5. What would you change for 100 times the data?**
Replace `skip` pagination with keyset pagination, keep daily and monthly rollups updated on write, store the department on each log to avoid the large `$in` in the trend query, and partition or shard by month.
