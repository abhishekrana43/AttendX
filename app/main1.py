"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import certifi
from datetime import datetime, timedelta, timezone, time, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field
from pymongo import MongoClient, ReturnDocument


load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                     tlsCAFile=certifi.where(),
                     serverSelectionTimeoutMS=10000,)
db = client[os.getenv("MONGO_DB", "AttendX")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))



def epoch_ms_to_utc(value: int) -> datetime:
    """Convert epoch milliseconds to a whole-second UTC BSON datetime."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, "Timestamp must be an integer in milliseconds")
    if value < 100_000_000_000 or value > 4_102_444_800_000:
        raise HTTPException(422, "Timestamp is outside the allowed range")

    seconds = value // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)


def utc_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    """MongoDB naive datetimes are interpreted as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp()) * 1000


def api_attendance(doc: dict) -> dict:
    """Serialize attendance without exposing MongoDB's internal _id."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": utc_to_epoch_ms(doc.get("punch_in")),
        "punch_out": utc_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [
            {
                **entry,
                "at": utc_to_epoch_ms(entry["at"]),
                "changes": {
                    key: {
                        "from": (
                            utc_to_epoch_ms(change["from"])
                            if key in ("punch_in", "punch_out")
                            and change.get("from") is not None
                            else change.get("from")
                        ),
                        "to": (
                            utc_to_epoch_ms(change["to"])
                            if key in ("punch_in", "punch_out")
                            and change.get("to") is not None
                            else change.get("to")
                        ),
                    }
                    for key, change in entry.get("changes", {}).items()
                },
            }
            for entry in doc.get("history", [])
        ],
    }


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def shift_end_datetime(emp: dict, attendance_date: str) -> datetime:
    """Return shift end in UTC-naive form, including overnight shifts."""
    start_h, start_m = map(int, emp["shift_start"].split(":"))
    end_h, end_m = map(int, emp["shift_end"].split(":"))

    day = date.fromisoformat(attendance_date)
    start = datetime.combine(day, time(start_h, start_m), tzinfo=IST)
    end = datetime.combine(day, time(end_h, end_m), tzinfo=IST)

    if (end_h, end_m) <= (start_h, start_m):
        end += timedelta(days=1)

    return end.astimezone(timezone.utc).replace(tzinfo=None)


def calculate_work_fields(emp: dict, attendance: dict) -> dict:
    punch_in = attendance.get("punch_in")
    punch_out = attendance.get("punch_out")

    if punch_in is None or punch_out is None:
        return {
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }

    seconds = (punch_out - punch_in).total_seconds()
    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(422, "Punch-out must be after punch-in and within 24 hours")

    hours = round_half_up(seconds / 3600, 2)
    shift_end = shift_end_datetime(emp, attendance["date"])
    overtime_seconds = (punch_out - shift_end).total_seconds()
    overtime_minutes = int(overtime_seconds // 60) if overtime_seconds >= 0 else 0

    return {
        "work_hours": hours,
        "overtime_minutes": overtime_minutes if overtime_minutes >= 30 else 0,
        "half_day": hours < 4.50,
    }
# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = page * page_size
    total = db.employees.count_documents({})
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()
    d = ts.date().isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    res = db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
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
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}



class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    ts = epoch_ms_to_utc(body.punched_at) if body.punched_at is not None else (
        now.replace(tzinfo=None)
    )

    # Locate the latest eligible record, including overnight shifts.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
        },
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Attendance record is already punched out")

    if ts <= record["punch_in"] or (ts - record["punch_in"]).total_seconds() > 86400:
        raise HTTPException(422, "Invalid punch-out time")

    fields = calculate_work_fields(
        emp,
        {**record, "punch_out": ts},
    )

    # Atomic conditional update: simultaneous requests cannot both succeed.
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "date": record["date"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                **fields,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(409, "Attendance record was already punched out")

    return api_attendance(updated)
# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
