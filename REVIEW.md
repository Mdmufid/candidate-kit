# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` | Always returns 200 without checking MongoDB. | Stop MongoDB and call `/health`. | Ping MongoDB and return 503 on failure. |
| 2 | `create_employee` | Duplicate protection is a check-then-insert race, and timestamps are naive. | Send concurrent creates or inspect the stored document. | Add a unique index and handle `DuplicateKeyError`; store UTC datetimes. |
| 3 | `list_employees` | `skip` starts at `page * page_size`, totals ignore the department filter, and results are not sorted. | Request page 1 or filter by department. | Use `(page - 1) * page_size`, filtered counts, and `emp_code` sorting. |
| 4 | `punch_in` | Unknown employees cause a server error; epoch values are treated as local seconds/milliseconds incorrectly; IST and overnight rules are missing; status is unchecked. | Punch an unknown employee, use an epoch-ms value near midnight, or use an overnight shift. | Validate input, convert epoch milliseconds through UTC, derive the IST attendance date, and use the unique attendance index. |
| 5 | `compute_late_minutes` / `compute_overtime` | Calculations use the wrong timezone/date basis and do not correctly support overnight shifts. | Test a punch near the grace boundary or an overnight punch-out. | Recompute from IST shift boundaries with whole-minute and grace-period rules. |
| 6 | `compute_work_hours` | Python `round` uses banker rounding rather than required half-up rounding. | Use a duration whose third decimal is exactly 5. | Use `Decimal` with `ROUND_HALF_UP`. |
| 7 | `list_attendance` | Loads and sorts the entire collection in Python, omits stable tie sorting, and mutates Mongo documents while serializing. | Seed a large collection and request a page. | Use indexed Mongo sorting, skip/limit, filtered count, and non-mutating serialization. |
| 8 | Missing endpoints | Punch-out, correction/audit, analytics, and explain routes are absent. | Call any route listed in `openapi.yaml` after `/attendance`. | Implemented the routes in `app/main.py`. |

Also note anything you looked at and decided was **not** a defect, and why.

The basic `compute_late_minutes` grace comparison was directionally correct for a same-day, naive timestamp, but it was not sufficient for the contract's IST and overnight rules. The starter's use of `emp_code` rather than Mongo `_id` was also correct and was preserved.
