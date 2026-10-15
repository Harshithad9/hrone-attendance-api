# REVIEW.md: review of the starter `app/main.py`

Each finding: where it is, what is wrong, how I noticed it (a concrete input or symptom), and how I fixed it.

## Defects found and fixed

| # | Where | What is wrong | How to notice it | Fix |
|---|---|---|---|---|
| 1 | `/health` | Always returns ok, never checks MongoDB. A commented-out duplicate of the function is left behind. | Stop MongoDB or use a wrong URI: it still answers 200. | Ping MongoDB; return 503 with `detail` on failure; removed the dead code. |
| 2 | `MongoClient(...)` | No `tz_aware`, so datetimes come back naive. No server-selection timeout (about 30 s hang when the database is down). | `created_at` returned as `2026-01-05T04:30:00` with no zone. | `tz_aware=True`, `serverSelectionTimeoutMS=3000`. |
| 3 | `EmployeeIn` | No validation at all. | `emp_code="abc"`, `shift_start="25:99"`, `joined_on="string"`, or `shift_start == shift_end` all return 201. | Patterns for code and times, length limits, real-date check, shifts must differ. 422 on failure. |
| 4 | `create_employee` | Check-then-insert race and no unique index. | Two identical POSTs sent together can both be saved. | Unique index on `emp_code` at startup; catch `DuplicateKeyError` and return 409. |
| 5 | `create_employee` | `created_at = datetime.now()` is local, naive time. PyMongo stores it as UTC, so the stored value is 5.5 h wrong. Returned as an ISO string, not epoch ms. | Response header said 06:47 GMT, `created_at` said 12:17. | UTC-aware time; `dt_to_ms()` at the API boundary. |
| 6 | `list_employees` | `total` is `count_documents({})`, ignoring the department filter. | `department=Engineering` gave 2 items but `total: 7`. | Count with the same filter as the items. |
| 7 | `list_employees` | No bounds on `page` / `page_size`, and no sort, so pages can overlap or skip rows. | `page=0`, `page=-1`, `page_size=1000` all accepted. | `page >= 1`, `1 <= page_size <= 100`, sort by `emp_code`. |
| 8 | `punch_in` | Unknown `emp_code` crashes on `emp["shift_start"]`. | `{"emp_code": "EMP9999"}` returns 500 with a plain-text body. | 404 with a `detail` message. |
| 9 | `punch_in` | `if body.punched_at:` treats `0` as "not given"; no range or type check, so floats and seconds are accepted. | `punched_at: 0` silently becomes "now"; `1791778200` (seconds) is accepted. | Strict integer epoch ms, at least 100000000000; otherwise 422. |
| 10 | `punch_in` | `date` is taken from `datetime.fromtimestamp()` / `.now()`, i.e. the server's local zone, not IST. Overnight shifts are not handled. | On a UTC server a 23:00 IST punch-in gets the wrong date. | `attendance_date()` converts to IST and applies the overnight rule R1. |
| 11 | `punch_in` | `status` accepts any string, including `ABSENT`, `LEAVE` and garbage. | `{"status": "ABSENT"}` returns 201. | Only `PRESENT`, `WFH`, `ON_DUTY` accepted. |
| 12 | `punch_in` | Check-then-insert race on (`emp_code`, `date`). | Two simultaneous requests can both return 201. | Unique compound index plus `DuplicateKeyError` to 409. |
| 13 | `punch_in` / `list_attendance` responses | Leak the internal `_id` as `id`; datetimes are not epoch ms; legacy documents without `half_day` / `history` come back incomplete. | Response contains `id` and ISO datetimes. | `log_out()` builds the contract shape with defaults and epoch ms. |
| 14 | `compute_late_minutes` | Uses `int(seconds / 60)` and then `> 10`, so 10 min 1 s becomes `10` and is not late. Builds the shift start from the punch-in's own zone and ignores overnight shifts. | 09:40:01 should give 10, the starter gives 0. | Work in whole seconds: late only if more than 600 s, then `secs // 60` from the shift start. |
| 15 | `compute_work_hours` | Python `round()` rounds half to even and works on a float. | 3618 s is exactly 1.005 h; `round()` gives 1.0, the rule needs 1.01. | `Decimal` with `ROUND_HALF_UP`. |
| 16 | `compute_overtime` | Missing the 30-minute minimum; assumes the shift ends on the punch date; subtracts an aware time from a naive one. | Punch-out 18:40 gives 10 overtime minutes (rule says 0); overnight shifts are wrong. | Whole minutes after shift end, only if at least 30; next-day end for overnight shifts; aware datetimes. |
| 17 | `list_attendance` | Loads every matching document, sorts and paginates in Python; no indexes; no validation of dates, status or `page_size`. | Slow on 100k records; `status=NONSENSE` returns 200; reversed dates return 200 with no rows. | Filter, sort, skip and limit in MongoDB; compound indexes; 422 on bad input; stable sort by `date` desc then `emp_code`. |

## Examined and judged fine

- `skip = (page - 1) * page_size` in `list_employees` is correct, but the comment above it (`skip = page * page_size`) is stale and misleading.
- `IST = timezone(timedelta(hours=5, minutes=30))` is the right offset.
- `load_dotenv()` does not override real environment variables, so `MONGO_URI` and `MONGO_DB` set in the shell still win.
- `{"_id": 0}` projection in `list_employees` correctly keeps `_id` out of the response.
