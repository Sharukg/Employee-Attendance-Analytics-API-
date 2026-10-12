# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"            # Windows PowerShell: $env:MONGO_URI="..."
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
A local `.env` with `MONGO_URI` / `MONGO_DB` also works; real environment variables take priority.
Indexes are created automatically at startup. Interactive docs: http://localhost:8000/docs

MongoDB 6.0+ is required (`$densify`, `$setWindowFields`, `$rank`).

## Status
Every endpoint in `openapi.yaml` is implemented, including `/admin/explain/{endpoint}`. Nothing is knowingly missing.

## Notes
- Part A findings are in `REVIEW.md`, design answers in `DECISIONS.md`.
- Analytics are single aggregation pipelines (`$lookup`, `$group`, `$setWindowFields`, `$densify`). Rounding is done half-up with integer or `Decimal` arithmetic, never float `round()`.
- Concurrency safety comes from unique indexes (punch-in, employee create) and compare-and-set updates (punch-out, regularize returns 409 for the loser).
