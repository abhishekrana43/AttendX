DECISIONS.md

1. Indexes

A unique `employees.emp_code` index prevents duplicate employee codes. A
unique `attendance_logs(emp_code, date)` index guarantees one daily
record per employee. The date/employee index supports date-range queries
and sorting; employee/punch-in helps find the latest eligible punch-in;
date/status supports filtered queries. Employee department/join-date
indexes support department summaries. I rejected a department index on
attendance logs because department belongs to employee records; trend
analytics joins employees instead.

 2. Punch-in race

Two requests can both pass the employee lookup and attempt the same
insert. The unique compound index allows one insert only. The winner
succeeds; the other raises `DuplicateKeyError`, converted to HTTP 409.
The database constraint provides the guarantee.

 3. Ties

The leaderboard calculates MongoDB `$rank` before filtering to
`rank <= limit`. Employees tied at the cutoff share that rank, so all
are returned, even if results exceed `limit`.

4. Headcount

The summary starts with employees who joined by month-end and looks up
monthly logs. `$unwind` with `preserveNullAndEmptyArrays: true` keeps
employees without logs in headcount.

 5. At 100x data

I would load-test aggregations, inspect execution plans, and tune
indexes to measured query patterns before adding caching or
pre-aggregated summaries.
