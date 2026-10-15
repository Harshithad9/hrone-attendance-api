# Employee Attendance & Analytics API

A FastAPI + MongoDB service for employee punch-in and punch-out, manual corrections with an audit trail, and analytics built on MongoDB aggregation pipelines. All code is in `app/main.py`.

## How to run

Requires Python 3.11+ and MongoDB 6.0+ (a local instance or a MongoDB Atlas cluster).

```
pip install -r requirements.txt
```

Set the two environment variables, then start the server.

PowerShell:
```
$env:MONGO_URI="mongodb://localhost:27017"
$env:MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

macOS / Linux:
```
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are read from the environment; a local `.env` file is also loaded if present. API docs are at `http://localhost:8000/docs`.

All indexes are created at startup, with no manual steps. If MongoDB is not reachable yet, the app still starts, `/health` returns 503, and the indexes are created as soon as the database answers.

## What is implemented

- **Part A:** the starter code was reviewed and fixed. Findings are in `REVIEW.md`.
- **Part B:** employees (create, list), punch-in, punch-out, regularization (`PATCH`) with a history entry per correction, attendance list, employee monthly summary, department summary, late leaderboard and department trend.
- **Part C:** `GET /admin/explain/{endpoint}` and the answers in `DECISIONS.md`.

## What was checked, and what to know

- The analytics endpoints and the explain endpoint were checked against the sample data on MongoDB Atlas. The explain output for all five endpoints shows an index scan (IXSCAN) and no collection scan (COLLSCAN).
- Punch-in, punch-out and regularization were tested with an in-memory mock of MongoDB, covering overnight shifts, the grace period, half-up rounding, history entries and error codes. They were not load-tested.
- Not tested on 100,000 records or with real concurrent requests. The concurrency guarantees rely on the unique indexes and on single atomic updates.
- The 7-day moving average in the trend uses the already-rounded `attendance_rate` values.
- A trend range counts as exceeding 92 days when it has more than 92 calendar days, inclusive.
- Gap-filling in the trend uses `$facet`, `$range` and `$map` inside the pipeline instead of `$densify`.