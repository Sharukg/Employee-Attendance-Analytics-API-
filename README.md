# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

Part A findings are in `REVIEW.md`, design answers in `DECISIONS.md`.

**Status:** every endpoint in `openapi.yaml` is implemented, including `/admin/explain/{endpoint}`. Nothing is knowingly missing.

## Requirements
- Python 3.10+ (developed against 3.10 and 3.13)
- MongoDB **6.0 or newer** (needed for `$densify`, `$setWindowFields`, `$rank`). Any of: a local MongoDB Community install (no Docker needed), Docker, or a free MongoDB Atlas cluster.

## 1. Run the API

Set the two environment variables, then start the server from the repository root.

Windows PowerShell:
```powershell
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
$env:MONGO_URI="mongodb://localhost:27017"
$env:MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

- A local `.env` file with `MONGO_URI` / `MONGO_DB` also works; real environment variables always take priority.
- All indexes are created automatically at startup (idempotent), no manual step.
- Wait for `Application startup complete.` and keep this window open.
- `http://127.0.0.1:8000/health` should return `{"status":"ok"}` (503 if MongoDB is not reachable).
- `http://127.0.0.1:8000/docs` is the interactive API page. There is no route at `/` (a 404 there is expected).
- Atlas: use the `mongodb+srv://...` connection string as `MONGO_URI` (quote it in PowerShell).

## 2. Try it with the sample data from the kit

Use a **throw-away database name**: `sample_seed.py` deletes all documents in `employees` and `attendance_logs` of `MONGO_DB`.

From the original candidate kit folder (it contains `sample_seed.py` and `sample_data/`), with the same `MONGO_URI` / `MONGO_DB` set:
```bash
python sample_seed.py            # loads 6 employees and 9 attendance records
```
(Equivalent with the MongoDB tools: `mongoimport --uri "$MONGO_URI" --db "$MONGO_DB" -c employees --jsonArray --drop --file sample_data/employees.json`, and the same for `attendance_logs`.)

Start the API (step 1) and open these URLs. Expected results on the sample:

| URL | Expected |
|---|---|
| `/attendance?page_size=100` | `total` 9, newest date first, then `emp_code`. The legacy record (EMP0002, 14 Jul) shows `half_day: false`, `history: []`. EMP0001 / 7 Jul shows one history entry whose punch times are epoch milliseconds. No `id` / `_id` anywhere. |
| `/analytics/employees/EMP0002/monthly?month=2026-07` | `working_days` 15 (joined 13 Jul), `present_days` 1.0, `attendance_pct` 6.67 |
| `/analytics/employees/EMP0003/monthly?month=2026-07` | `present_days` 1.0, `leave_days` 1, `late_count` 1, `total_late_minutes` 35, `total_overtime_minutes` 40 |
| `/analytics/employees/EMP0004/monthly?month=2026-07` | `present_days` 0.5 (WFH half day), `attendance_pct` 2.17 |
| `/analytics/departments/summary?month=2026-07` | Engineering: headcount 2, present_days 3.0, avg_work_hours 9.04. Sales: headcount 2, present_days 1.5, avg 6.29, late_count 1, leave_count 1. Support: headcount 2, present_days 2.0, avg 8.75 |
| `/analytics/leaderboard/late?month=2026-07` | one row: rank 1, EMP0003, 35 minutes |
| `/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-14` | 9 rows, one per day; weekend rows have `attendance_rate: null`; headcount goes 1 -> 2 on 13 Jul; `moving_avg_7d` on 14 Jul is 0.1 |
| `/admin/explain/department_summary?month=2026-07` | explain JSON containing `IXSCAN` and no `COLLSCAN` |

Business-rule examples (shift 09:30): punch-in 09:40:00 -> `late_minutes` 0, 09:40:01 -> 10, 10:15:59 -> 45. Overnight shift 22:00-06:00, punch-in 21:55, punch-out 06:40 next day -> `date` is the day the shift started, 8.75 hours, 40 overtime minutes.

## 3. Automated self-check (`check_local.py`)

The sample has only nine documents, so passing it proves little. `check_local.py` builds its own awkward dataset (employees with no logs, mid-month joiners, a joiner after month end, weekend records, missing legacy fields, a log of an unknown employee, exact ties) and compares every analytics endpoint with an independent pure-Python calculation. It also fires parallel requests and checks the explain plans.

With the API running (step 1) in one window, open a second window with the **same** `MONGO_URI` / `MONGO_DB` and run:
```bash
pip install requests pymongo
python check_local.py          # correctness + concurrency + explain
python tools/check_local.py --big    # also loads ~100k logs, prints timings, re-checks explain
```
It **empties** `employees` and `attendance_logs` in `MONGO_DB` (keeps the indexes), so use a throw-away database. Expected last line: `ALL CHECKS PASSED`.

What it verifies:
- monthly / department summary / late leaderboard / trend equal the reference results,
- leaderboard ties rank as 1, 2, 2, 4, 4, 6,
- 20 parallel identical punch-ins -> exactly one 201 and 19 x 409; 20 parallel punch-outs -> exactly one 200; 10 parallel employee creates -> exactly one 201,
- concurrent corrections never lose a history entry (history length == number of 200 responses),
- `/admin/explain/*` for all five endpoints contains `IXSCAN` and no `COLLSCAN`.

## Design notes
- Analytics are single aggregation pipelines (`$lookup`, `$group`, `$setWindowFields`, `$densify`); nothing is looped in Python except small calendar maths and final rounding.
- Rounding is half-up with integer or `Decimal` arithmetic, never float `round()`.
- Concurrency: unique indexes (punch-in, employee create) and compare-and-set updates (punch-out; regularize returns 409 for the loser).
- Instants are stored as UTC BSON datetimes truncated to whole seconds and exposed as epoch milliseconds.
