"""
Employee Attendance & Analytics API.

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,                  # datetimes come back as UTC-aware
    serverSelectionTimeoutMS=3000,  # fail fast instead of hanging ~30 s
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


_indexes_ready = False


def ensure_indexes() -> None:
    """Create the indexes once. Uniqueness is enforced by the database, not by
    check-then-insert, and the analytics/list queries depend on the rest."""
    global _indexes_ready
    if _indexes_ready:
        return
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING)])
    _indexes_ready = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    # If MongoDB is not reachable yet, do not crash: /health reports 503 and the
    # indexes are created as soon as the database answers.
    try:
        ensure_indexes()
    except PyMongoError as exc:
        print(f"WARNING: indexes not created at startup ({type(exc).__name__}); will retry")
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Time helpers (instants <-> epoch ms, IST dates)
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC-aware datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Datetime -> integer epoch milliseconds. None stays None."""
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat a naive value as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def round_half_up(x, places: int = 2) -> float:
    q = Decimal(1).scaleb(-places)  # Decimal('0.01') for places=2
    return float(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Business rules R1-R5 (pure functions, no database access)
# --------------------------------------------------------------------------- #
def is_overnight(shift_start: str, shift_end: str) -> bool:
    # "HH:MM" strings compare correctly as text.
    return shift_end <= shift_start


def at_ist(date_str: str, hhmm: str) -> datetime:
    """The instant at which `hhmm` (IST) occurs on `date_str` (IST)."""
    y, mo, d = map(int, date_str.split("-"))
    h, m = map(int, hhmm.split(":"))
    return datetime(y, mo, d, h, m, tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST date of the punch-in; an overnight punch-in earlier than
    shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    if is_overnight(shift_start, shift_end) and local.strftime("%H:%M") < shift_end:
        local -= timedelta(days=1)
    return local.date().isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then the
    whole minutes since shift start (not since the end of the grace period)."""
    secs = int((punch_in - at_ist(date_str, shift_start)).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_overtime(
    punch_out: datetime, shift_start: str, shift_end: str, date_str: str
) -> int:
    """R3: whole minutes after shift end, counted only when >= 30."""
    end = at_ist(date_str, shift_end)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)  # the shift ends on the next calendar day
    secs = int((punch_out - end).total_seconds())
    minutes = secs // 60 if secs > 0 else 0
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 decimals, using exact Decimal maths."""
    secs = int((punch_out - punch_in).total_seconds())
    hours = Decimal(secs) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_half_day(work_hours: float) -> bool:
    """R5: uses the already-rounded work_hours."""
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Models (validation follows the schemas in openapi.yaml)
# --------------------------------------------------------------------------- #
TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"


def check_real_date(v: str) -> str:
    date.fromisoformat(v)  # raises ValueError for 2026-13-45 etc.
    return v


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")  # e.g. a client-sent created_at is ignored

    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=TIME_PATTERN)
    shift_end: str = Field("18:30", pattern=TIME_PATTERN)
    joined_on: str = Field(pattern=DATE_PATTERN)

    @field_validator("joined_on")
    @classmethod
    def joined_on_is_a_real_date(cls, v: str) -> str:
        return check_real_date(v)

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")  # derived fields sent by a client are ignored

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def no_explicit_nulls(self):
        # Omitted means "unchanged"; an explicit null is not a valid value.
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


# --------------------------------------------------------------------------- #
# Response shaping: BSON documents -> API JSON (no _id, instants as epoch ms)
# --------------------------------------------------------------------------- #
def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": dt_to_ms(d.get("created_at")),
    }


def _history_out(h: dict) -> dict:
    changes = {}
    for field, ft in (h.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):  # BSON dates nested in the object
            ft = {"from": dt_to_ms(ft.get("from")), "to": dt_to_ms(ft.get("to"))}
        changes[field] = ft
    return {"at": dt_to_ms(h.get("at")), "by": h.get("by"),
            "reason": h.get("reason"), "changes": changes}


def log_out(d: dict) -> dict:
    # Legacy documents may lack late_minutes / overtime_minutes / half_day / history.
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")),
        "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": [_history_out(h) for h in (d.get("history") or [])],
    }


def field_error(where: str, param: str, msg: str) -> RequestValidationError:
    """A 422 in FastAPI's standard validation-error shape."""
    return RequestValidationError([{"type": "value_error", "loc": (where, param),
                                    "msg": msg, "input": None}])


def query_error(param: str, msg: str) -> RequestValidationError:
    return field_error("query", param, msg)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    try:
        ensure_indexes()  # no-op once done; covers a database that came up after the app
    except PyMongoError as exc:
        print(f"WARNING: could not create indexes ({type(exc).__name__})")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index decides, even under concurrency
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)  # same filter as the items (R10)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [employee_out(d) for d in cursor],
            "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return log_out(doc)


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    """The filter for GET /attendance. Shared with /admin/explain so both agree."""
    for name, value in (("date_from", date_from), ("date_to", date_to)):
        if value is not None:
            try:
                check_real_date(value)
            except ValueError:
                raise query_error(name, "not a valid calendar date")
    if date_from and date_to and date_from > date_to:
        raise query_error("date_from", "date_from must not be after date_to")
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [log_out(d) for d in cursor],
            "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # The most recent record whose punch_in is at or before punched_at. Looking at
    # punch_in (not at the date) is what makes overnight shifts work across midnight.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"] or ts - rec["punch_in"] > timedelta(hours=24):
        raise field_error("body", "punched_at",
                          "must be after punch_in and within 24 hours of it")

    wh = compute_work_hours(rec["punch_in"], ts)
    # One atomic update: the filter only matches while the record is still open,
    # so of two simultaneous punch-outs exactly one succeeds.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": wh,
            "overtime_minutes": compute_overtime(
                ts, emp["shift_start"], emp["shift_end"], rec["date"]),
            "half_day": is_half_day(wh),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return log_out(updated)


ABSENCE = ("ABSENT", "LEAVE")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(body: RegularizeIn, emp_code: str, date: str = Path(pattern=DATE_PATTERN)):
    try:
        check_real_date(date)
    except ValueError:
        raise field_error("path", "date", "not a valid calendar date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    new_status = body.status or rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")

    # ---- the final state after the correction ----
    if new_status in ABSENCE:
        if body.punch_in is not None or body.punch_out is not None:
            raise field_error("body", "status", "ABSENT/LEAVE cannot carry punch times")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise field_error("body", "punch_in", "a presence status needs a punch_in")
        if body.punch_in is not None and attendance_date(
                new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise field_error("body", "punch_in", "must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise field_error("body", "punch_out",
                              "must be after punch_in and within 24 hours of it")

    # ---- recompute every derived field from the final state (R2-R5) ----
    if new_in is None:
        late, wh, ot, half = 0, None, 0, False
    else:
        late = compute_late_minutes(new_in, emp["shift_start"], date)
        if new_out is None:
            wh, ot, half = None, 0, False
        else:
            wh = compute_work_hours(new_in, new_out)
            ot = compute_overtime(new_out, emp["shift_start"], emp["shift_end"], date)
            half = is_half_day(wh)

    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                "work_hours": wh, "late_minutes": late,
                "overtime_minutes": ot, "half_day": half}
    old_vals = {"status": rec["status"], "punch_in": old_in, "punch_out": old_out,
                "work_hours": rec.get("work_hours"),
                "late_minutes": rec.get("late_minutes") or 0,
                "overtime_minutes": rec.get("overtime_minutes") or 0,
                "half_day": bool(rec.get("half_day", False))}
    changes = {k: {"from": old_vals[k], "to": new_vals[k]}
               for k in new_vals if new_vals[k] != old_vals[k]}
    if not changes:
        raise field_error("body", "reason", "the request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by,
             "reason": body.reason, "changes": changes}

    # Optimistic concurrency: apply only if nobody changed this record since we read
    # it (history length and punch_out are what a concurrent writer would change).
    history_len = len(rec.get("history") or [])
    guard = {"_id": rec["_id"], "punch_out": old_out}
    guard["$or"] = ([{"history": {"$exists": False}}, {"history": {"$size": 0}}]
                    if history_len == 0 else [{"history": {"$size": history_len}}])
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": new_vals, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, please retry")
    return log_out(updated)


# --------------------------------------------------------------------------- #
# Analytics: every figure is computed by a MongoDB aggregation pipeline.
# Each pipeline is built by a function that both the endpoint and
# /admin/explain call, so the explained query is exactly the one that runs.
# --------------------------------------------------------------------------- #
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]          # R6
WEEKDAY_DOW = [2, 3, 4, 5, 6]                     # $dayOfWeek: 1 = Sunday ... 7 = Saturday

_LATE = {"$ifNull": ["$late_minutes", 0]}
_IS_PRESENCE = {"$in": ["$status", PRESENCE]}
_IS_HALF = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
_HAS_HOURS = {"$and": [_IS_PRESENCE, {"$isNumber": "$work_hours"}]}
_ON_WEEKDAY = {"$in": [
    {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}},
    WEEKDAY_DOW,
]}


def month_bounds(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31')."""
    y, m = int(month[:4]), int(month[5:7])
    return f"{month}-01", f"{month}-{calendar.monthrange(y, m)[1]:02d}"


def count_weekdays(start: str, end: str) -> int:
    """Mon-Fri days between two YYYY-MM-DD strings, inclusive (R7). 0 if start > end."""
    d, last = date.fromisoformat(start), date.fromisoformat(end)
    n = 0
    while d <= last:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def pct(part: float, whole: int) -> Optional[float]:
    if whole == 0:
        return None
    v = Decimal(str(part)) * 100 / Decimal(whole)
    return float(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def log_stats_group() -> dict:
    """Accumulators shared by the employee and department summaries."""
    return {
        "present_days": {"$sum": {"$cond": [
            {"$and": [_IS_PRESENCE, _ON_WEEKDAY]},          # R7: Mon-Fri only
            {"$cond": [_IS_HALF, 0.5, 1]}, 0]}},             # R5: half day = 0.5
        "wh_sum": {"$sum": {"$cond": [_HAS_HOURS, "$work_hours", 0]}},
        "wh_cnt": {"$sum": {"$cond": [_HAS_HOURS, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": _LATE},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
    }


def monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": None, **log_stats_group()}},
    ]


def summary_pipeline(month: str, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last}}            # R9: headcount
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        # Start from employees so people with zero logs still count. The sub-pipeline
        # collapses each employee's month into one stats document.
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$group": {"_id": None, **log_stats_group()}},
            ],
            "as": "stats",
        }},
        {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            **{k: {"$sum": {"$ifNull": [f"$stats.{k}", 0]}} for k in (
                "present_days", "wh_sum", "wh_cnt", "late_count",
                "total_late_minutes", "leave_count", "on_duty_count")},
        }},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": _LATE},
            "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},                                # logs of unknown employees drop out
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department
    pipeline += [
        # $rank is standard competition ranking: ties share a rank, the next is skipped.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},              # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def trend_pipeline(department: str, frm: date, to: date) -> list:
    employees = list(db.employees.find({"department": department},
                                       {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not employees:
        raise HTTPException(404, "unknown department")
    codes = [e["emp_code"] for e in employees]
    joins = [e["joined_on"] for e in employees]
    days = (to - frm).days + 1
    from_dt = datetime(frm.year, frm.month, frm.day, tzinfo=timezone.utc)
    half_units = {"$cond": [_IS_PRESENCE, {"$cond": [_IS_HALF, 1, 2]}, 0]}  # present_count * 2

    one_day = {"$let": {
        "vars": {"d": {"$dateAdd": {"startDate": from_dt, "unit": "day", "amount": "$$i"}}},
        "in": {"$let": {
            "vars": {"ds": {"$dateToString": {"date": "$$d", "format": "%Y-%m-%d"}},
                     "dow": {"$dayOfWeek": "$$d"}},
            "in": {"$let": {
                "vars": {
                    "row": {"$arrayElemAt": [{"$filter": {
                        "input": "$rows", "as": "r",
                        "cond": {"$eq": ["$$r._id", "$$ds"]}}}, 0]},
                    "hc": {"$size": {"$filter": {                      # R9: joined by that day
                        "input": {"$literal": joins}, "as": "j",
                        "cond": {"$lte": ["$$j", "$$ds"]}}}},
                },
                "in": {"date": "$$ds", "dow": "$$dow", "headcount": "$$hc",
                       "p2": {"$ifNull": ["$$row.p2", 0]},
                       "late_count": {"$ifNull": ["$$row.late_count", 0]}},
            }},
        }},
    }}

    return [
        {"$match": {"emp_code": {"$in": codes},
                    "date": {"$gte": frm.isoformat(), "$lte": to.isoformat()}}},
        # $facet yields one document even when nothing matched, so every day still gets a row.
        {"$facet": {"rows": [{"$group": {
            "_id": "$date",
            "p2": {"$sum": half_units},
            "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
        }}]}},
        # Gap filling inside the database: one row for each calendar day in the range.
        {"$project": {"_id": 0, "days": {"$map": {
            "input": {"$range": [0, days]}, "as": "i", "in": one_day}}}},
        {"$unwind": "$days"},
        {"$replaceRoot": {"newRoot": "$days"}},
        {"$set": {"is_working_day": {"$in": ["$dow", WEEKDAY_DOW]}}},
        # attendance_rate x 10000 as an integer, rounded half-up with exact integer maths:
        # floor((p2 * 10000 + headcount) / (2 * headcount)).
        {"$set": {"rate_n": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": ["$p2", 10000]}, "$headcount"]},
                {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        # 7-row window (current + 6 preceding) inside the requested range only.
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "sum_n": {"$sum": "$rate_n", "window": {"documents": [-6, 0]}},
            "cnt": {"$sum": {"$cond": [{"$ne": ["$rate_n", None]}, 1, 0]},
                    "window": {"documents": [-6, 0]}},
        }}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "late_count": 1,
            "present_count": {"$divide": ["$p2", 2]},
            "attendance_rate": {"$cond": [{"$eq": ["$rate_n", None]}, None,
                                          {"$divide": ["$rate_n", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$gt": ["$cnt", 0]}, {"$divide": [
                {"$floor": {"$divide": [
                    {"$add": [{"$multiply": ["$sum_n", 2]}, "$cnt"]},
                    {"$multiply": ["$cnt", 2]}]}}, 10000]}, None]},
        }},
        {"$sort": {"date": 1}},
    ]


def parse_day(param: str, value: str) -> date:
    if not re.fullmatch(DATE_PATTERN, value):
        raise query_error(param, "expected YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise query_error(param, "not a valid calendar date")


def checked_trend_range(frm: str, to: str) -> tuple[date, date]:
    d_from, d_to = parse_day("from", frm), parse_day("to", to)
    if d_to < d_from:
        raise query_error("to", "to must not be before from")
    if (d_to - d_from).days + 1 > 92:
        raise query_error("to", "range must not exceed 92 days")
    return d_from, d_to


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first, last = month_bounds(month)
    working_days = count_weekdays(max(first, emp["joined_on"]), last)  # R7: from joined_on
    result = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)))
    s = result[0] if result else {}
    present = float(s.get("present_days", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present,
        "leave_days": int(s.get("leave_count", 0)),
        "late_count": int(s.get("late_count", 0)),
        "total_late_minutes": int(s.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(s.get("total_overtime_minutes", 0)),
        "attendance_pct": pct(present, working_days),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN),
                       department: Optional[str] = None):
    items = []
    for r in db.employees.aggregate(summary_pipeline(month, department)):
        wh_cnt = int(r["wh_cnt"])
        avg = None
        if wh_cnt:
            v = Decimal(str(round(float(r["wh_sum"]), 6))) / Decimal(wh_cnt)
            avg = float(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=MONTH_PATTERN),
                     limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None):
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": [
        {"rank": int(i["rank"]), "emp_code": i["emp_code"], "name": i["name"],
         "department": i["department"], "total_late_minutes": int(i["total_late_minutes"]),
         "late_count": int(i["late_count"])} for i in items]}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,
                     date_from: str = Query(..., alias="from"),
                     date_to: str = Query(..., alias="to")):
    d_from, d_to = checked_trend_range(date_from, date_to)
    rows = db.attendance_logs.aggregate(trend_pipeline(department, d_from, d_to))
    return {"department": department, "items": [
        {"date": r["date"], "is_working_day": bool(r["is_working_day"]),
         "headcount": int(r["headcount"]), "present_count": float(r["present_count"]),
         "late_count": int(r["late_count"]), "attendance_rate": r["attendance_rate"],
         "moving_avg_7d": r["moving_avg_7d"]} for r in rows]}


# --------------------------------------------------------------------------- #
# Admin: explain the main query of an endpoint, built by the same functions.
# --------------------------------------------------------------------------- #
def run_explain(command: dict) -> dict:
    command["verbosity"] = "executionStats"
    return json.loads(json_util.dumps(db.command(command)))


def need(**params):
    for name, value in params.items():
        if value is None:
            raise query_error(name, "required for this endpoint")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    trend_from: Optional[str] = Query(None, alias="from"),
    trend_to: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        ensure_indexes()
    except PyMongoError:
        pass
    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        cmd = {"find": collection, "filter": q, "sort": dict(ATTENDANCE_SORT),
               "skip": (page - 1) * page_size, "limit": page_size}
    elif endpoint == "employee_monthly":
        need(emp_code=emp_code, month=month)
        collection = "attendance_logs"
        cmd = {"aggregate": collection, "pipeline": monthly_pipeline(emp_code, month), "cursor": {}}
    elif endpoint == "department_summary":
        need(month=month)
        collection = "employees"
        cmd = {"aggregate": collection, "pipeline": summary_pipeline(month, department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        need(month=month)
        collection = "attendance_logs"
        cmd = {"aggregate": collection,
               "pipeline": leaderboard_pipeline(month, limit, department), "cursor": {}}
    else:  # department_trend
        need(department=department, **{"from": trend_from, "to": trend_to})
        d_from, d_to = checked_trend_range(trend_from, trend_to)
        collection = "attendance_logs"
        cmd = {"aggregate": collection,
               "pipeline": trend_pipeline(department, d_from, d_to), "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": run_explain({"explain": cmd})}