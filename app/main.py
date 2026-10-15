# """
# Employee Attendance & Analytics API - STARTER

# Run:  uvicorn app.main:app --port 8000
# Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

# This file was written quickly by a colleague who has left the company. The happy path
# works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
# openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
# """
# import os
# from datetime import datetime, timedelta, timezone
# from typing import Optional

# from dotenv import load_dotenv
# from fastapi import FastAPI, HTTPException
# from pydantic import BaseModel
# from pymongo import MongoClient

# load_dotenv()

# client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
# db = client[os.getenv("MONGO_DB", "attendance_db")]

# IST = timezone(timedelta(hours=5, minutes=30))

# app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# # --------------------------------------------------------------------------- #
# # Helpers
# # --------------------------------------------------------------------------- #
# def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
#     h, m = map(int, shift_start.split(":"))
#     start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
#     minutes = int((punch_in - start).total_seconds() / 60)
#     return minutes if minutes > 10 else 0


# def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
#     return round((punch_out - punch_in).total_seconds() / 3600, 2)


# def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
#     h, m = map(int, shift_end.split(":"))
#     end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
#     return max(0, int((punch_out - end).total_seconds() / 60))


# # --------------------------------------------------------------------------- #
# # Models
# # --------------------------------------------------------------------------- #
# class EmployeeIn(BaseModel):
#     emp_code: str
#     name: str
#     email: str
#     department: str
#     shift_start: str = "09:30"
#     shift_end: str = "18:30"
#     joined_on: str


# class PunchInIn(BaseModel):
#     emp_code: str
#     punched_at: Optional[int] = None
#     status: str = "PRESENT"


# # --------------------------------------------------------------------------- #
# # Endpoints provided
# # --------------------------------------------------------------------------- #
# # @app.get("/health")
# # def health():
# #     return {"status": "ok"}

# @app.get("/health")
# def health():
#     return {"status": "ok"}


# @app.post("/employees", status_code=201)
# def create_employee(body: EmployeeIn):
#     if db.employees.find_one({"emp_code": body.emp_code}):
#         raise HTTPException(409, "emp_code already exists")
#     doc = body.model_dump()
#     doc["created_at"] = datetime.now()
#     db.employees.insert_one(doc)
#     doc.pop("_id", None)
#     return doc


# @app.get("/employees")
# def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
#     q = {}
#     if department:
#         q["department"] = department
#     # skip = page * page_size
#     skip = (page - 1) * page_size
#     total = db.employees.count_documents({})
#     items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
#     return {"items": items, "total": total, "page": page, "page_size": page_size}


# @app.post("/attendance/punch-in", status_code=201)
# def punch_in(body: PunchInIn):
#     emp = db.employees.find_one({"emp_code": body.emp_code})
#     ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()
#     d = ts.date().isoformat()
#     if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
#         raise HTTPException(409, "already punched in for this date")
#     doc = {
#         "emp_code": body.emp_code,
#         "date": d,
#         "status": body.status,
#         "punch_in": ts,
#         "punch_out": None,
#         "work_hours": None,
#         "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
#         "overtime_minutes": 0,
#         "half_day": False,
#         "history": [],
#     }
#     res = db.attendance_logs.insert_one(doc)
#     doc["id"] = str(res.inserted_id)
#     doc.pop("_id", None)
#     return doc


# @app.get("/attendance")
# def list_attendance(
#     emp_code: Optional[str] = None,
#     date_from: Optional[str] = None,
#     date_to: Optional[str] = None,
#     status: Optional[str] = None,
#     page: int = 1,
#     page_size: int = 20,
# ):
#     q = {}
#     if emp_code:
#         q["emp_code"] = emp_code
#     if date_from or date_to:
#         q["date"] = {}
#         if date_from:
#             q["date"]["$gte"] = date_from
#         if date_to:
#             q["date"]["$lte"] = date_to
#     if status:
#         q["status"] = status
#     docs = list(db.attendance_logs.find(q))
#     docs.sort(key=lambda d: d["date"], reverse=True)
#     total = len(docs)
#     page_docs = docs[(page - 1) * page_size : page * page_size]
#     for d in page_docs:
#         d["id"] = str(d.pop("_id"))
#     return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


# # --------------------------------------------------------------------------- #
# # TODO - the rest of the contract (see openapi.yaml):
# #   POST  /attendance/punch-out
# #   PATCH /attendance/{emp_code}/{date}
# #   GET   /analytics/employees/{emp_code}/monthly
# #   GET   /analytics/departments/summary
# #   GET   /analytics/leaderboard/late
# #   GET   /analytics/departments/{department}/trend
# #   GET   /admin/explain/{endpoint}
# # --------------------------------------------------------------------------- #

































"""
Employee Attendance & Analytics API.

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Uniqueness is enforced by the database, not by check-then-insert.
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
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


def query_error(param: str, msg: str) -> RequestValidationError:
    """A 422 in FastAPI's standard validation-error shape."""
    return RequestValidationError([{"type": "value_error", "loc": ("query", param),
                                    "msg": msg, "input": None}])


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
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


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
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

    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [log_out(d) for d in cursor],
            "total": total, "page": page, "page_size": page_size}