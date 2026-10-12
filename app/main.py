"""
Employee Attendance & Analytics API
FastAPI + MongoDB (PyMongo)
Implements openapi.yaml specification & R1-R10 business rules.
"""

import math
import os
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

# Real environment variables take precedence over .env
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000


# --------------------------------------------------------------------------- #
# Startup & Lifespan: Indexes Creation
# --------------------------------------------------------------------------- #
def ensure_indexes():
    # 1. employees: emp_code must be unique
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")
    # Department filter and index scans
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_emp_dept_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="idx_emp_joined_on")

    # 2. attendance_logs: composite unique natural key (emp_code, date)
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="idx_att_emp_date_unique"
    )
    # attendance listing & pagination: sorted by date DESC, emp_code ASC
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING), ("status", ASCENDING)],
        name="idx_att_date_desc_emp_status",
    )
    # employee monthly & punch-out lookups
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="idx_att_emp_punch_in",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Math & Business Logic Helpers (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(value: float, decimals: int) -> float:
    d = Decimal(str(value))
    target = Decimal("10") ** (-decimals)
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def epoch_ms_to_utc_dt(epoch_ms: int) -> datetime:
    # R1: Truncate to whole seconds before any calculation
    truncated_sec = epoch_ms // 1000
    return datetime.fromtimestamp(truncated_sec, tz=UTC)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 1000


def parse_hh_mm(time_str: str) -> dt_time:
    h, m = map(int, time_str.split(":"))
    return dt_time(hour=h, minute=m)


def get_shift_instants(punch_in_utc: datetime, shift_start_str: str, shift_end_str: str):
    """
    R1: Computes the record date (YYYY-MM-DD IST), shift_start instant (UTC),
    and shift_end instant (UTC).
    """
    in_ist = punch_in_utc.astimezone(IST)
    start_time = parse_hh_mm(shift_start_str)
    end_time = parse_hh_mm(shift_end_str)
    is_overnight = end_time <= start_time

    # Overnight shift: if punch-in IST time < shift_end, it belongs to the previous calendar day
    if is_overnight and in_ist.time() < end_time:
        rec_date = in_ist.date() - timedelta(days=1)
    else:
        rec_date = in_ist.date()

    start_dt_ist = datetime.combine(rec_date, start_time, tzinfo=IST)
    shift_start_utc = start_dt_ist.astimezone(UTC)

    if is_overnight:
        end_dt_ist = datetime.combine(rec_date + timedelta(days=1), end_time, tzinfo=IST)
    else:
        end_dt_ist = datetime.combine(rec_date, end_time, tzinfo=IST)
    shift_end_utc = end_dt_ist.astimezone(UTC)

    return rec_date.isoformat(), shift_start_utc, shift_end_utc


def compute_late_minutes_r2(punch_in_utc: datetime, shift_start_utc: datetime) -> int:
    """
    R2: Grace period is 10 min. Late strictly if punch_in > shift_start + 10m.
    late_minutes = floor((punch_in - shift_start) in seconds / 60)
    """
    diff_sec = int((punch_in_utc - shift_start_utc).total_seconds())
    if diff_sec > 600:
        return math.floor(diff_sec / 60)
    return 0


def compute_overtime_r3(punch_out_utc: datetime, shift_end_utc: datetime) -> int:
    """
    R3: whole minutes (floor) between shift_end and punch_out, only if >= 30 min.
    """
    diff_sec = int((punch_out_utc - shift_end_utc).total_seconds())
    if diff_sec >= 1800:
        return math.floor(diff_sec / 60)
    return 0


def compute_work_hours_r4(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded half-up to 2 decimals.
    """
    total_sec = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(total_sec / 3600.0, 2)


def compute_half_day_r5(rounded_work_hours: Optional[float]) -> bool:
    """
    R5: true when rounded work_hours < 4.50.
    """
    if rounded_work_hours is None:
        return False
    return rounded_work_hours < 4.50


def validate_epoch_ms(val: Optional[int]) -> Optional[int]:
    if val is None:
        return None
    if not isinstance(val, int) or isinstance(val, bool):
        raise HTTPException(status_code=422, detail="Epoch milliseconds must be an integer")
    if val < MIN_EPOCH_MS or val > MAX_EPOCH_MS:
        raise HTTPException(status_code=422, detail="Invalid epoch milliseconds range")
    return val


def format_doc_to_api(doc: Dict[str, Any]) -> Dict[str, Any]:
    history_api = []
    for h in doc.get("history", []) or []:
        h_changes = {}
        for k, v in h.get("changes", {}).items():
            f_val = v.get("from")
            t_val = v.get("to")
            if isinstance(f_val, datetime):
                f_val = dt_to_epoch_ms(f_val)
            if isinstance(t_val, datetime):
                t_val = dt_to_epoch_ms(t_val)
            h_changes[k] = {"from": f_val, "to": t_val}

        history_api.append({
            "at": dt_to_epoch_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": h_changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history_api,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")

    @field_validator("punched_at")
    @classmethod
    def check_punched_at(cls, v):
        return validate_epoch_ms(v)


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def check_punched_at(cls, v):
        return validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_instants(cls, v):
        return validate_epoch_ms(v)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")

    doc = body.model_dump()
    created_at_dt = datetime.now(tz=UTC)
    doc["created_at"] = created_at_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(created_at_dt),
    }


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)

    items = []
    for d in cursor:
        d["created_at"] = dt_to_epoch_ms(d.get("created_at"))
        items.append(d)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_in_utc = (
        epoch_ms_to_utc_dt(body.punched_at)
        if body.punched_at is not None
        else datetime.now(tz=UTC).replace(microsecond=0)
    )

    rec_date, shift_start_utc, _ = get_shift_instants(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes_r2(punch_in_utc, shift_start_utc)

    doc = {
        "emp_code": body.emp_code,
        "date": rec_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for that date")

    return format_doc_to_api(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    punch_out_utc = (
        epoch_ms_to_utc_dt(body.punched_at)
        if body.punched_at is not None
        else datetime.now(tz=UTC).replace(microsecond=0)
    )

    # Find the most recent record whose punch_in <= punched_at
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", DESCENDING)],
    )

    if not candidate:
        raise HTTPException(status_code=404, detail="No punch-in found")

    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")

    punch_in_utc = candidate["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=UTC)

    diff_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_seconds <= 0 or diff_seconds > 86400:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")

    _, _, shift_end_utc = get_shift_instants(punch_in_utc, emp["shift_start"], emp["shift_end"])
    work_hours = compute_work_hours_r4(punch_in_utc, punch_out_utc)
    overtime_min = compute_overtime_r3(punch_out_utc, shift_end_utc)
    half_day = compute_half_day_r5(work_hours)

    # Atomic compare-and-swap to guarantee race-condition safety
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=True,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="record is already punched out")

    return format_doc_to_api(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_doc_to_api(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    old_status = rec["status"]
    old_punch_in = rec.get("punch_in")
    old_punch_out = rec.get("punch_out")
    old_work_hours = rec.get("work_hours")
    old_late_minutes = rec.get("late_minutes", 0)
    old_overtime_minutes = rec.get("overtime_minutes", 0)
    old_half_day = bool(rec.get("half_day", False))

    new_status = body.status if body.status is not None else old_status

    # ABSENT / LEAVE validation
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Punch times cannot be supplied for ABSENT/LEAVE")
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_punch_in = epoch_ms_to_utc_dt(body.punch_in) if body.punch_in is not None else old_punch_in
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a punch_in")

        r1_date, shift_start_utc, shift_end_utc = get_shift_instants(
            new_punch_in, emp["shift_start"], emp["shift_end"]
        )
        if r1_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record attendance date")

        new_late_minutes = compute_late_minutes_r2(new_punch_in, shift_start_utc)

        new_punch_out = epoch_ms_to_utc_dt(body.punch_out) if body.punch_out is not None else old_punch_out
        if new_punch_out is not None:
            diff = (new_punch_out - new_punch_in).total_seconds()
            if diff <= 0 or diff > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")
            new_work_hours = compute_work_hours_r4(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime_r3(new_punch_out, shift_end_utc)
            new_half_day = compute_half_day_r5(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    changes = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="Request made no changes to the record")

    now_utc = datetime.now(tz=UTC).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic locking using history length to prevent concurrent loss of history
    current_history_len = len(rec.get("history", []) or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "history": {"$size": current_history_len},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=True,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent update conflict")

    return format_doc_to_api(updated)


# --------------------------------------------------------------------------- #
# Analytics Pipelines
# --------------------------------------------------------------------------- #
def count_working_days(start_date: dt_date, end_date: dt_date) -> int:
    days = 0
    cur = start_date
    while cur <= end_date:
        if cur.weekday() < 5:  # Monday = 0, Friday = 4
            days += 1
        cur += timedelta(days=1)
    return days


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    month_start = dt_date(year, m, 1)
    next_m = dt_date(year + 1, 1, 1) if m == 12 else dt_date(year, m + 1, 1)
    month_end = next_m - timedelta(days=1)

    emp_joined = dt_date.fromisoformat(emp["joined_on"])
    effective_start = max(month_start, emp_joined)

    if effective_start > month_end:
        working_days = 0
    else:
        working_days = count_working_days(effective_start, month_end)

    start_date_str = month_start.isoformat()
    end_date_str = month_end.isoformat()

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date_str, "$lte": end_date_str},
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                    }
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$day_of_week", 2]},
                                    {"$lte": ["$day_of_week", 6]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    attendance_pct = (
        round_half_up((present_days / working_days) * 100.0, 2)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    next_m = dt_date(year + 1, 1, 1) if m == 12 else dt_date(year, m + 1, 1)
    month_end_date = next_m - timedelta(days=1)
    month_end_str = month_end_date.isoformat()
    month_start_str = f"{month}-01"

    match_stage = {"joined_on": {"$lte": month_end_str}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start_str]},
                                    {"$lte": ["$date", month_end_str]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "log": "$logs",
                "day_of_week": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {
                                    "dateString": "$logs.date",
                                    "format": "%Y-%m-%d",
                                }
                            }
                        },
                        0,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$day_of_week", 2]},
                                    {"$lte": ["$day_of_week", 6]},
                                ]
                            },
                            {"$cond": ["$log.half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$log.work_hours", None]},
                                ]
                            },
                            "$log.work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$log.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$log.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$log.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$log.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$log.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        headcount = len(r.get("employees", []))
        if headcount == 0:
            continue
        w_count = r.get("work_hours_count", 0)
        avg_w = (
            round_half_up(r.get("work_hours_sum", 0.0) / w_count, 2)
            if w_count > 0
            else None
        )
        items.append({
            "department": r["_id"],
            "headcount": headcount,
            "present_days": float(r.get("present_days", 0.0)),
            "avg_work_hours": avg_w,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, m = map(int, month.split("-"))
    next_m = dt_date(year + 1, 1, 1) if m == 12 else dt_date(year, m + 1, 1)
    month_end_date = next_m - timedelta(days=1)

    month_start_str = f"{month}-01"
    month_end_str = month_end_date.isoformat()

    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start_str, "$lte": month_end_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$sort": {
                "total_late_minutes": DESCENDING,
                "emp_code": ASCENDING,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
    ])

    results = list(db.attendance_logs.aggregate(pipeline))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in results
    ]

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        f_dt = dt_date.fromisoformat(from_date)
        t_dt = dt_date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if t_dt < f_dt:
        raise HTTPException(status_code=422, detail="'to' must be greater than or equal to 'from'")
    if (t_dt - f_dt).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # 404 if department has zero employees
    emp_count = db.employees.count_documents({"department": department})
    if emp_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    dept_employees = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))

    # MongoDB aggregation using $densify for gap-filling
    pipeline = [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "date_dt": {
                    "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                },
            }
        },
        {
            "$group": {
                "_id": "$date_dt",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "date_dt": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(f_dt, dt_time.min, tzinfo=UTC),
                        datetime.combine(t_dt + timedelta(days=1), dt_time.min, tzinfo=UTC),
                    ],
                },
            }
        },
        {"$sort": {"date_dt": ASCENDING}},
    ]

    cursor = list(db.attendance_logs.aggregate(pipeline))
    agg_map = {
        r["date_dt"].strftime("%Y-%m-%d"): {
            "present_count": float(r.get("present_count") or 0.0),
            "late_count": int(r.get("late_count") or 0),
        }
        for r in cursor
        if "date_dt" in r and r["date_dt"] is not None
    }

    # Generate complete series & 7-day moving average calculation
    items = []
    window_rates: List[float] = []

    cur = f_dt
    while cur <= t_dt:
        cur_str = cur.isoformat()
        is_working_day = cur.weekday() < 5
        headcount = sum(1 for e in dept_employees if e["joined_on"] <= cur_str)

        counts = agg_map.get(cur_str, {"present_count": 0.0, "late_count": 0})
        present_count = counts["present_count"]
        late_count = counts["late_count"]

        if is_working_day and headcount > 0:
            rate = round_half_up(present_count / headcount, 4)
            window_rates.append(rate)
        else:
            rate = None

        # Keep sliding window within last 7 days inside the range
        if len(window_rates) > 7:
            window_rates.pop(0)

        # 7-day moving average of non-null attendance_rate
        if window_rates:
            m_avg = round_half_up(sum(window_rates) / len(window_rates), 4)
        else:
            m_avg = None

        items.append({
            "date": cur_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": rate,
            "moving_avg_7d": m_avg,
        })
        cur += timedelta(days=1)

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        skip = (page - 1) * page_size
        cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}}},
            {"$group": {"_id": None, "count": {"$sum": 1}}},
        ]
        cmd = {
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        match_stage = {"joined_on": {"$lte": f"{month}-31"}}
        if department:
            match_stage["department"] = department
        pipeline = [
            {"$match": match_stage},
            {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}},
        ]
        cmd = {
            "explain": {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        return {"endpoint": endpoint, "collection": "employees", "explain": res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = [
            {"$match": {"date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}},
        ]
        cmd = {
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        ]
        cmd = {
            "explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            "verbosity": "executionStats",
        }
        res = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    raise HTTPException(status_code=422, detail="Invalid endpoint")