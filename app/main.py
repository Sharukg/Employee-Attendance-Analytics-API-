"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)

Layout of this single file:
  1. configuration / database / indexes
  2. pure helpers (time zone, R1-R5 rules, rounding, serialisation)
  3. request models
  4. query / pipeline builders (shared by the real endpoints and /admin/explain)
  5. endpoints
"""
import calendar
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration, database, indexes
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
StatusT = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

# index names are fixed so that queries can hint them explicitly
IX_LOG_EMP_DATE = "emp_code_1_date_1"
IX_LOG_DATE_EMP = "date_-1_emp_code_1"
IX_LOG_EMP_PUNCH = "emp_code_1_punch_in_-1"
IX_EMP_CODE = "emp_code_1"
IX_EMP_DEPT_CODE = "department_1_emp_code_1"
IX_EMP_DEPT_JOINED = "department_1_joined_on_1"
IX_EMP_JOINED = "joined_on_1"

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: creating an index that already exists with the same spec is a no-op."""
    global _indexes_ready
    logs, emps = db.attendance_logs, db.employees
    # the unique indexes are what make the races safe
    emps.create_index([("emp_code", 1)], name=IX_EMP_CODE, unique=True)
    logs.create_index([("emp_code", 1), ("date", 1)], name=IX_LOG_EMP_DATE, unique=True)
    logs.create_index([("date", -1), ("emp_code", 1)], name=IX_LOG_DATE_EMP)
    logs.create_index([("emp_code", 1), ("punch_in", -1)], name=IX_LOG_EMP_PUNCH)
    emps.create_index([("department", 1), ("emp_code", 1)], name=IX_EMP_DEPT_CODE)
    emps.create_index([("department", 1), ("joined_on", 1)], name=IX_EMP_DEPT_JOINED)
    emps.create_index([("joined_on", 1)], name=IX_EMP_JOINED)
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Mongo may still be starting: retry for a few seconds, never crash the app.
    for _attempt in range(5):
        try:
            ensure_indexes()
            break
        except PyMongoError as exc:  # pragma: no cover
            log.warning("index creation failed (%s); retrying", exc)
            time.sleep(1)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# 2. Helpers
# --------------------------------------------------------------------------- #
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


def to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def utc_now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """BSON datetime -> epoch milliseconds (integer arithmetic, no float rounding)."""
    if dt is None:
        return None
    return (aware(dt) - EPOCH) // timedelta(milliseconds=1)


def parse_date(value: str, name: str = "date") -> date:
    if not DATE_RE.fullmatch(value):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid calendar date")


def month_bounds(month: str) -> tuple[date, date]:
    try:
        year, mon = int(month[:4]), int(month[5:7])
        first = date(year, mon, 1)
        return first, date(year, mon, calendar.monthrange(year, mon)[1])
    except (ValueError, IndexError):
        raise HTTPException(422, "month must be YYYY-MM")


def hhmm_secs(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 3600 + int(m) * 60


def is_overnight(emp: dict) -> bool:
    return hhmm_secs(emp["shift_end"]) <= hhmm_secs(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch belongs to the previous day."""
    local = aware(punch_in).astimezone(IST)
    day = local.date()
    if is_overnight(emp):
        secs_of_day = local.hour * 3600 + local.minute * 60 + local.second
        if secs_of_day < hhmm_secs(emp["shift_end"]):
            day -= timedelta(days=1)
    return day.isoformat()


def shift_start_at(date_str: str, emp: dict) -> datetime:
    h, m = map(int, emp["shift_start"].split(":"))
    return datetime.combine(date.fromisoformat(date_str), dtime(h, m), tzinfo=IST)


def shift_end_at(date_str: str, emp: dict) -> datetime:
    h, m = map(int, emp["shift_end"].split(":"))
    end = datetime.combine(date.fromisoformat(date_str), dtime(h, m), tzinfo=IST)
    return end + timedelta(days=1) if is_overnight(emp) else end


def _secs(delta: timedelta) -> int:
    return delta.days * 86400 + delta.seconds  # inputs are whole seconds, so this is exact


def compute_late_minutes(punch_in: datetime, date_str: str, emp: dict) -> int:
    """R2: late only when strictly more than 10:00 after shift start; then floor minutes since shift start."""
    secs = _secs(aware(punch_in) - shift_start_at(date_str, emp))
    return secs // 60 if secs > 600 else 0


def compute_overtime(punch_out: datetime, date_str: str, emp: dict) -> int:
    """R3: floor minutes after shift end, only if at least 30."""
    secs = _secs(aware(punch_out) - shift_end_at(date_str, emp))
    mins = secs // 60  # floor, also for negative values
    return mins if mins >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals HALF-UP (Decimal, not float round())."""
    secs = _secs(aware(punch_out) - aware(punch_in))
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derived_fields(status: str, punch_in, punch_out, date_str: str, emp: dict) -> dict:
    """Everything the API computes (R2-R5) for a record's final state."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "late_minutes": compute_late_minutes(punch_in, date_str, emp),
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, date_str, emp)
        out["half_day"] = out["work_hours"] < 4.50
    return out


def round_half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def weekdays_between(start: date, end: date) -> int:
    n, d = 0, start
    while d <= end:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


# ---- serialisation ---------------------------------------------------------
def history_out(entry: dict) -> dict:
    changes = {}
    for key, ch in (entry.get("changes") or {}).items():
        if key in ("punch_in", "punch_out"):
            ch = {"from": to_ms(ch.get("from")), "to": to_ms(ch.get("to"))}
        changes[key] = ch
    return {"at": to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Stored attendance document -> API shape. Missing legacy fields get their defaults; no _id."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 3. Request models
# --------------------------------------------------------------------------- #
TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_date(cls, v: str) -> str:
        if not DATE_RE.fullmatch(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(v)  # raises ValueError for 2026-02-30 etc.
        return v

    @model_validator(mode="after")
    def _shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[StatusT] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 4. Query / pipeline builders (shared by endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
Page = Annotated[int, Query(ge=1, le=1_000_000_000)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def attendance_list_query(emp_code, date_from, date_to, status, page, page_size) -> dict:
    flt: dict[str, Any] = {}
    if emp_code is not None:
        flt["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        lo = parse_date(date_from, "date_from") if date_from is not None else None
        hi = parse_date(date_to, "date_to") if date_to is not None else None
        if lo and hi and lo > hi:
            raise HTTPException(422, "date_from must not be after date_to")
        flt["date"] = {}
        if lo:
            flt["date"]["$gte"] = lo.isoformat()
        if hi:
            flt["date"]["$lte"] = hi.isoformat()
    if status is not None:
        flt["status"] = status
    return {
        "filter": flt,
        "sort": [("date", -1), ("emp_code", 1)],
        "skip": (page - 1) * page_size,
        "limit": page_size,
        "hint": IX_LOG_EMP_DATE if emp_code is not None else IX_LOG_DATE_EMP,
    }


def _present_weight2() -> dict:
    """Twice the present-day weight of a log (so it stays an integer): 0 absent, 1 half day, 2 full day."""
    return {"$cond": [{"$in": ["$status", PRESENCE]},
                      {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}, 0]}


def _weekday_only(expr: dict) -> dict:
    """expr on Mon-Fri records, 0 on weekend records ($dayOfWeek: 1 = Sunday ... 7 = Saturday)."""
    dow = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
    return {"$cond": [{"$in": [dow, [2, 3, 4, 5, 6]]}, expr, 0]}


def _late_flag() -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}


def monthly_pipeline(emp_code: str, month: str) -> dict:
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present2": {"$sum": _weekday_only(_present_weight2())},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": _late_flag()},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    return {"collection": "attendance_logs", "pipeline": pipeline, "hint": IX_LOG_EMP_DATE}


def summary_pipeline(month: str, department: Optional[str]) -> dict:
    first, last = month_bounds(month)
    emp_match: dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}  # R9
    if department is not None:
        emp_match["department"] = department
    # per employee, the month's logs are folded into ONE small document inside the $lookup,
    # so employees without any log still flow through the pipeline (and are counted).
    per_employee = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present2": {"$sum": _weekday_only(_present_weight2())},
            "wh_cent": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", PRESENCE]}, {"$isNumber": "$work_hours"}]},
                {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
            "wh_n": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$status", PRESENCE]}, {"$isNumber": "$work_hours"}]}, 1, 0]}},
            "late_count": {"$sum": _late_flag()},
            "late_total": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": per_employee, "as": "agg"}},
        {"$set": {"agg": {"$arrayElemAt": ["$agg", 0]}}},  # missing when the employee has no logs
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present2": {"$sum": "$agg.present2"},
            "wh_cent": {"$sum": "$agg.wh_cent"},
            "wh_n": {"$sum": "$agg.wh_n"},
            "late_count": {"$sum": "$agg.late_count"},
            "late_total": {"$sum": "$agg.late_total"},
            "leave": {"$sum": "$agg.leave"},
            "on_duty": {"$sum": "$agg.on_duty"},
        }},
        {"$sort": {"_id": 1}},
    ]
    hint = IX_EMP_DEPT_JOINED if department is not None else IX_EMP_JOINED
    return {"collection": "employees", "pipeline": pipeline, "hint": hint}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> dict:
    first, last = month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return {"collection": "attendance_logs", "pipeline": pipeline, "hint": IX_LOG_DATE_EMP}


def trend_pipeline(department: str, frm: date, to: date) -> dict:
    frm_s, to_s = frm.isoformat(), to.isoformat()
    day = lambda d: datetime(d.year, d.month, d.day, tzinfo=UTC)  # noqa: E731
    # one small document per (employee, event); event = "joined on day X" or "log on day X"
    logs_of_employee = [
        {"$match": {"date": {"$gte": frm_s, "$lte": to_s}}},
        {"$project": {"_id": 0, "date": 1, "p2": _present_weight2(), "late": _late_flag()}},
    ]
    joined_after_range = {"$gt": ["$joined_on", to_s]}
    pipeline: list[dict] = [
        {"$match": {"department": department}},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": logs_of_employee, "as": "logs"}},
        {"$project": {"_id": 0, "events": {"$concatArrays": [
            [{"date": {"$cond": [joined_after_range, frm_s, {"$max": ["$joined_on", frm_s]}]},
              "joined": {"$cond": [joined_after_range, 0, 1]}}],
            "$logs"]}}},
        {"$unwind": "$events"},
        {"$replaceRoot": {"newRoot": "$events"}},
        {"$group": {"_id": "$date", "joined": {"$sum": "$joined"}, "p2": {"$sum": "$p2"}, "late": {"$sum": "$late"}}},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id"}}}},
        # gap filling: one document for every calendar day; the upper bound is exclusive
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day",
                                              "bounds": [day(frm), day(to) + timedelta(days=1)]}}},
        {"$set": {"joined": {"$ifNull": ["$joined", 0]}, "p2": {"$ifNull": ["$p2", 0]},
                  "late": {"$ifNull": ["$late", 0]}}},
        # headcount on day X = employees that joined on or before X = running total of "joined"
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "headcount": {"$sum": "$joined", "window": {"documents": ["unbounded", "current"]}}}}},
        {"$set": {"is_working_day": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]}}},
        # rate in units of 1/10000, rounded half-up with integer arithmetic only:
        # floor((p2/2/hc)*10000 + 0.5) == floor((p2*10000 + hc) / (2*hc))
        {"$set": {"rate_int": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$p2", 10000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        {"$set": {"rate_nn": {"$cond": [{"$eq": ["$rate_int", None]}, 0, 1]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "w_sum": {"$sum": "$rate_int", "window": {"documents": [-6, "current"]}},
            "w_n": {"$sum": "$rate_nn", "window": {"documents": [-6, "current"]}}}}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": {"$divide": ["$p2", 2]},
            "late_count": "$late",
            "attendance_rate": {"$cond": [{"$eq": ["$rate_int", None]}, None, {"$divide": ["$rate_int", 10000]}]},
            # mean of the non-null (rounded) rates in the window, again half-up: floor((2S + n) / 2n)
            "moving_avg_7d": {"$cond": [{"$gt": ["$w_n", 0]},
                                        {"$divide": [{"$floor": {"$divide": [
                                            {"$add": [{"$multiply": [2, "$w_sum"]}, "$w_n"]},
                                            {"$multiply": [2, "$w_n"]}]}}, 10000]},
                                        None]},
        }},
        {"$sort": {"date": 1}},
    ]
    return {"collection": "employees", "pipeline": pipeline, "hint": IX_EMP_DEPT_JOINED}


def run_aggregate(spec: dict) -> list[dict]:
    return list(db[spec["collection"]].aggregate(spec["pipeline"], hint=spec["hint"]))


def parse_trend_range(frm: str, to: str) -> tuple[date, date]:
    d_from, d_to = parse_date(frm, "from"), parse_date(to, "to")
    if d_to < d_from:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    return d_from, d_to


# --------------------------------------------------------------------------- #
# 5. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# ---- employees --------------------------------------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now = datetime.now(UTC)
    doc = body.model_dump()
    doc["created_at"] = now.replace(microsecond=now.microsecond // 1000 * 1000)  # BSON keeps milliseconds
    try:
        db.employees.insert_one(doc)  # unique index on emp_code decides the winner of a race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    flt = {"department": department} if department is not None else {}
    total = db.employees.count_documents(flt)
    cursor = db.employees.find(flt, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# ---- attendance -------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = to_dt(body.punched_at) if body.punched_at is not None else utc_now_seconds()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, day, emp),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    out = to_dt(body.punched_at) if body.punched_at is not None else utc_now_seconds()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out}},  # null punch_in (ABSENT/LEAVE) never matches
        sort=[("punch_in", -1)],
        hint=IX_LOG_EMP_PUNCH,
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = aware(rec["punch_in"])
    if out <= p_in or out - p_in > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after the punch-in and within 24 hours of it")
    fields = derived_fields(rec["status"], p_in, out, rec["date"], emp)
    fields["punch_out"] = out
    # compare-and-set: only succeeds while the record is still open and unchanged since we read it
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"], "status": rec["status"]},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out or modified concurrently")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
    col = db.attendance_logs
    total = col.count_documents(q["filter"]) if q["filter"] else col.estimated_document_count()
    cursor = col.find(q["filter"], {"_id": 0}, sort=q["sort"], skip=q["skip"], limit=q["limit"], hint=q["hint"])
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date")
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old_status = rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"{new_status} records cannot have punch times")
        new_in = new_out = None
    else:
        new_in = to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if attendance_date(new_in, emp) != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (aware(new_out) <= aware(new_in) or aware(new_out) - aware(new_in) > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if (new_status, new_in, new_out) == (old_status, old_in, old_out):
        raise HTTPException(422, "request changes nothing")

    new_values = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                  **derived_fields(new_status, new_in, new_out, date, emp)}
    old_values = {
        "status": old_status, "punch_in": old_in, "punch_out": old_out,
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old_values[k], "to": new_values[k]} for k in new_values if old_values[k] != new_values[k]}
    entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # optimistic lock: the update only applies if the editable fields and the history length are exactly
    # what we read. A concurrent edit makes this match nothing -> 409, so no history entry is overwritten.
    guard: dict[str, Any] = {"emp_code": emp_code, "date": date, "status": old_status,
                             "punch_in": old_in, "punch_out": old_out}
    guard["history"] = {"$size": len(rec["history"])} if rec.get("history") is not None else {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": new_values, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; reload and retry")
    return record_out(updated)


# ---- analytics --------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[str, Query(pattern=MONTH_PATTERN)]):
    first, last = month_bounds(month)
    emp = get_employee(emp_code)
    rows = run_aggregate(monthly_pipeline(emp_code, month))
    agg = rows[0] if rows else {}
    joined = date.fromisoformat(emp["joined_on"])
    working_days = weekdays_between(max(first, joined), last)  # R7: counted from joined_on
    present2 = int(agg.get("present2", 0))
    pct = None
    if working_days:
        pct = round_half_up(Decimal(present2) / Decimal(2 * working_days) * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present2 / 2,
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[str, Query(pattern=MONTH_PATTERN)], department: Optional[str] = None):
    items = []
    for r in run_aggregate(summary_pipeline(month, department)):
        wh_n = int(r["wh_n"])
        avg = round_half_up(Decimal(int(r["wh_cent"])) / Decimal(wh_n) / 100, 2) if wh_n else None
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": int(r["present2"]) / 2,
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["late_total"]),
            "leave_count": int(r["leave"]),
            "on_duty_count": int(r["on_duty"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[str, Query(pattern=MONTH_PATTERN)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    items = run_aggregate(leaderboard_pipeline(month, limit, department))
    return {"month": month, "items": [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
        for r in items]}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from")],
    to: str,
):
    d_from, d_to = parse_trend_range(from_, to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    rows = run_aggregate(trend_pipeline(department, d_from, d_to))
    return {"department": department, "items": [
        {"date": r["date"], "is_working_day": bool(r["is_working_day"]), "headcount": int(r["headcount"]),
         "present_count": float(r["present_count"]), "late_count": int(r["late_count"]),
         "attendance_rate": r["attendance_rate"], "moving_avg_7d": r["moving_avg_7d"]}
        for r in rows]}


# ---- admin: explain ---------------------------------------------------------
def _jsonable(doc: Any) -> Any:
    return json.loads(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS))


def _need(value: Any, name: str) -> Any:
    if value is None:
        raise HTTPException(422, f"query parameter '{name}' is required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        q = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command: dict[str, Any] = {"find": collection, "filter": q["filter"], "projection": {"_id": 0},
                                   "sort": dict(q["sort"]), "skip": q["skip"], "limit": q["limit"], "hint": q["hint"]}
    else:
        if endpoint == "employee_monthly":
            spec = monthly_pipeline(_need(emp_code, "emp_code"), _need(month, "month"))
        elif endpoint == "department_summary":
            spec = summary_pipeline(_need(month, "month"), department)
        elif endpoint == "late_leaderboard":
            spec = leaderboard_pipeline(_need(month, "month"), limit, department)
        else:
            d_from, d_to = parse_trend_range(_need(from_, "from"), _need(to, "to"))
            spec = trend_pipeline(_need(department, "department"), d_from, d_to)
        collection = spec["collection"]
        command = {"aggregate": collection, "pipeline": spec["pipeline"], "cursor": {}, "hint": spec["hint"]}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": _jsonable(result)}
