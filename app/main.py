import os
import calendar
from datetime import datetime, timedelta, timezone, date, time
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import certifi
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tlsCAFile=certifi.where(),
    serverSelectionTimeoutMS=10000,
)
db = client[os.getenv("MONGO_DB", "AttendX")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
STATUSES = PRESENCE + ["ABSENT", "LEAVE"]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ------------------------------- utilities ----------------------------------
def ensure_indexes():
    db.employees.create_index([("emp_code", 1)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="ix_employee_department_joined")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="ix_attendance_emp_punchin")
    db.attendance_logs.create_index([("date", 1), ("status", 1)], name="ix_attendance_date_status")


@app.on_event("startup")
def startup():
    ensure_indexes()


def parse_day(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise HTTPException(422, "Date must use YYYY-MM-DD")


def month_bounds(month: str):
    try:
        if len(month) != 7 or month[4] != "-":
            raise ValueError
        year, mon = map(int, month.split("-"))
        first = date(year, mon, 1)
        last = date(year, mon, calendar.monthrange(year, mon)[1])
        return first.isoformat(), last.isoformat(), first, last
    except (ValueError, TypeError):
        raise HTTPException(422, "month must use YYYY-MM")


def epoch_ms_to_utc(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, "Timestamp must be integer epoch milliseconds")
    if value < 100_000_000_000 or value > 4_102_444_800_000:
        raise HTTPException(422, "Timestamp is outside the allowed range")
    return datetime.fromtimestamp(value // 1000, tz=UTC).replace(tzinfo=None)


def utc_to_epoch_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def round_half_up(value, places=2):
    if value is None:
        return None
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def shift_start_dt(emp, day):
    h, m = map(int, emp["shift_start"].split(":"))
    return datetime.combine(date.fromisoformat(day), time(h, m), tzinfo=IST).astimezone(UTC).replace(tzinfo=None)


def shift_end_dt(emp, day):
    sh, sm = map(int, emp["shift_start"].split(":"))
    eh, em = map(int, emp["shift_end"].split(":"))
    d = date.fromisoformat(day)
    end = datetime.combine(d, time(eh, em), tzinfo=IST)
    if (eh, em) <= (sh, sm):
        end += timedelta(days=1)
    return end.astimezone(UTC).replace(tzinfo=None)


def attendance_day(ts, emp):
    local = ts.replace(tzinfo=UTC).astimezone(IST)
    sh, sm = map(int, emp["shift_start"].split(":"))
    eh, em = map(int, emp["shift_end"].split(":"))
    if (eh, em) <= (sh, sm) and (local.hour, local.minute) < (eh, em):
        local -= timedelta(days=1)
    return local.date().isoformat()


def late_minutes(punch_in, emp, day):
    seconds = (punch_in - shift_start_dt(emp, day)).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def work_fields(emp, record):
    pi, po = record.get("punch_in"), record.get("punch_out")
    if pi is None or po is None:
        return {"work_hours": None, "overtime_minutes": 0, "half_day": False}
    secs = (po - pi).total_seconds()
    if secs <= 0 or secs > 86400:
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    hours = round_half_up(secs / 3600, 2)
    over = int(((po - shift_end_dt(emp, record["date"])).total_seconds()) // 60)
    return {"work_hours": hours, "overtime_minutes": over if over >= 30 else 0, "half_day": hours < 4.5}


def api_employee(doc):
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"], "joined_on": doc["joined_on"],
        "created_at": utc_to_epoch_ms(doc["created_at"]),
    }


def api_attendance(doc):
    history = []
    for entry in doc.get("history", []):
        changes = {}
        for key, change in entry.get("changes", {}).items():
            old, new = change.get("from"), change.get("to")
            if key in ("punch_in", "punch_out"):
                old, new = utc_to_epoch_ms(old), utc_to_epoch_ms(new)
            changes[key] = {"from": old, "to": new}
        history.append({"at": utc_to_epoch_ms(entry["at"]), "by": entry["by"],
                        "reason": entry["reason"], "changes": changes})
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": utc_to_epoch_ms(doc.get("punch_in")),
        "punch_out": utc_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False), "history": history,
    }


def check_status(status):
    if status not in STATUSES:
        raise HTTPException(422, "Invalid attendance status")


# --------------------------------- models ------------------------------------
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift(cls, value):
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError:
            raise ValueError("Shift time must use HH:MM")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        parse_day(value)
        return value


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def valid_presence(cls, value):
        if value not in PRESENCE:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[int] = None


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value is not None and value not in STATUSES:
            raise ValueError("Invalid status")
        return value


# -------------------------------- endpoints ----------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB is unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return api_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [api_employee(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    ts = epoch_ms_to_utc(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    day = attendance_day(ts, emp)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status,
           "punch_in": ts, "punch_out": None, "work_hours": None,
           "late_minutes": late_minutes(ts, emp, day), "overtime_minutes": 0,
           "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")
    return api_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    ts = epoch_ms_to_utc(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
                                         sort=[("punch_in", -1)])
    if not record:
        raise HTTPException(404, "No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    if ts <= record["punch_in"] or (ts - record["punch_in"]).total_seconds() > 86400:
        raise HTTPException(422, "Invalid punch-out time")
    derived = work_fields(emp, {**record, "punch_out": ts})
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": record["date"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "Already punched out")
    return api_attendance(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[str] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if status is not None:
        check_status(status)
    if date_from:
        parse_day(date_from)
    if date_to:
        parse_day(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    q = {}
    if emp_code is not None:
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
    cur = db.attendance_logs.find(q, {"_id": 0}).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [api_attendance(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    parse_day(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(404, "Attendance record not found")
    new = dict(old)
    if body.status is not None:
        new["status"] = body.status
    for field in ("punch_in", "punch_out"):
        val = getattr(body, field)
        if val is not None:
            new[field] = epoch_ms_to_utc(val)
    if new["status"] in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot include punch times")
        new["punch_in"] = new["punch_out"] = None
    else:
        if new.get("punch_in") is None:
            raise HTTPException(422, "Presence status requires punch_in")
        if attendance_day(new["punch_in"], emp) != date:
            raise HTTPException(422, "punch_in must belong to attendance date")
    if new.get("punch_out") is not None:
        if new.get("punch_in") is None:
            raise HTTPException(422, "punch_out requires punch_in")
        secs = (new["punch_out"] - new["punch_in"]).total_seconds()
        if secs <= 0 or secs > 86400:
            raise HTTPException(422, "Invalid punch_out")
    new["late_minutes"] = late_minutes(new["punch_in"], emp, date) if new.get("punch_in") is not None and new["status"] in PRESENCE else 0
    new.update(work_fields(emp, new))
    changed_fields = ["status", "punch_in", "punch_out", "late_minutes", "work_hours", "overtime_minutes", "half_day"]
    changes = {}
    for field in changed_fields:
        before, after = old.get(field), new.get(field)
        if before != after:
            changes[field] = {"from": before, "to": after}
    if not changes:
        raise HTTPException(422, "Request does not change the record")
    entry = {"at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
             "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # CAS on current history prevents concurrent updates from overwriting history.
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": emp_code, "date": date, "history": old.get("history", [])},
        {"$set": {k: new.get(k) for k in changed_fields}, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "Record changed concurrently; retry")
    return api_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    first_s, last_s, first, last = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "Employee not found")
    joined = parse_day(emp["joined_on"])
    start = max(first, joined)
    working = sum(1 for n in range((last - start).days + 1) if (start + timedelta(days=n)).weekday() < 5) if start <= last else 0
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_s, "$lte": last_s}}},
        {"$addFields": {"dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", PRESENCE]}, {"$in": ["$dow", [2,3,4,5,6]]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"}}}
    ]
    out = list(db.attendance_logs.aggregate(pipeline))
    s = out[0] if out else {}
    present = round_half_up(s.get("present_days", 0), 2)
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": present,
            "leave_days": s.get("leave_days", 0), "late_count": s.get("late_count", 0),
            "total_late_minutes": s.get("total_late_minutes", 0),
            "total_overtime_minutes": s.get("total_overtime_minutes", 0),
            "attendance_pct": round_half_up(present / working * 100, 2) if working else None}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                       department: Optional[str] = None):
    first_s, last_s, _, _ = month_bounds(month)
    pipeline = [
        {"$match": {"joined_on": {"$lte": last_s}, **({"department": department} if department is not None else {})}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
          "pipeline": [{"$match": {"$expr": {"$and": [
              {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first_s]}, {"$lte": ["$date", last_s]}]}}}],
          "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {"dow": {"$cond": [{"$ne": ["$logs", None]}, {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, None]}}},
        {"$group": {"_id": "$department", "codes": {"$addToSet": "$emp_code"},
          "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", PRESENCE]}, {"$in": ["$dow", [2,3,4,5,6]]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
          "hours": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", PRESENCE]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}},
          "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
          "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
          "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
          "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$codes"}, "present_days": 1,
          "avg_work_hours": {"$avg": "$hours"}, "late_count": 1, "total_late_minutes": 1,
          "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]
    items = list(db.employees.aggregate(pipeline))
    for item in items:
        item["present_days"] = round_half_up(item["present_days"], 2)
        item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first_s, last_s, _, _ = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first_s, "$lte": last_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1,
                      "total_late_minutes": 1, "late_count": 1}}
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"),
                     to_date: str = Query(..., alias="to")):
    start, end = parse_day(from_date), parse_day(to_date)
    if end < start or (end - start).days + 1 > 92:
        raise HTTPException(422, "Invalid date range; to must be >= from and range <= 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")
    # Seed the requested range in MongoDB so even a range with no logs gets every date.
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$group": {"_id": "$date",
          "present_count": {"$sum": {"$cond": [{"$in": ["$status", PRESENCE]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
          "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}}},
        {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day",
            "bounds": [datetime.combine(start, time.min), datetime.combine(end, time.min)]}}},
        {"$fill": {"output": {"present_count": {"value": 0}, "late_count": {"value": 0}}}},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
          "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$day"}, 6]}]}}},
        {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}]}}},
            {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]} }},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}}
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    # Some MongoDB versions densify only between observed rows; fill missing boundary/empty dates.
    by_day = {r["date"]: r for r in rows}
    output = []
    cursor = start
    rates = []
    while cursor <= end:
        key = cursor.isoformat()
        row = by_day.get(key)
        if row is None:
            hc = db.employees.count_documents({"department": department, "joined_on": {"$lte": key}})
            working = cursor.weekday() < 5
            rate = 0.0 if working and hc > 0 else None
            row = {"date": key, "is_working_day": working, "headcount": hc, "present_count": 0,
                   "late_count": 0, "attendance_rate": rate}
        rate = row.get("attendance_rate")
        rates.append(rate)
        window = [x for x in rates[-7:] if x is not None]
        moving = sum(window) / len(window) if window else None
        row["present_count"] = round_half_up(row.get("present_count", 0), 2)
        row["attendance_rate"] = round_half_up(rate, 4)
        row["moving_avg_7d"] = round_half_up(moving, 4)
        output.append(row)
        cursor += timedelta(days=1)
    return {"department": department, "items": output}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    request: Request,
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
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
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, f"endpoint must be one of {sorted(allowed)}")
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                parse_day(date_from)
                q["date"]["$gte"] = date_from
            if date_to:
                parse_day(date_to)
                q["date"]["$lte"] = date_to
            if date_from and date_to and date_from > date_to:
                raise HTTPException(422, "date_from must be <= date_to")
        if status:
            check_status(status)
            q["status"] = status
        explain = db.command("explain", {"find": "attendance_logs", "filter": q,
            "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size},
            verbosity="executionStats")
        collection = "attendance_logs"
    else:
        if endpoint in {"employee_monthly"} and (not emp_code or not month):
            raise HTTPException(422, "emp_code and month are required")
        if endpoint in {"department_summary", "late_leaderboard"} and not month:
            raise HTTPException(422, "month is required")
        #if endpoint == "department_trend" and (not department or not from_date or not to_date):
            #raise HTTPException(422, "department, from, and to are required")
        
        elif endpoint == "department_trend":
            if not department or not from_date or not to_date:
                raise HTTPException(
                    status_code=422,
                    detail="department, from, and to are required"
                )

            try:
                start = parse_day(from_date)
                end = parse_day(to_date)

                if end < start or (end - start).days + 1 > 92:
                    raise HTTPException(
                        status_code=422,
                        detail="Invalid trend range"
                    )

                pipeline = [
                    {
                        "$match": {
                            "date": {
                                "$gte": start.isoformat(),
                                "$lte": end.isoformat()
                            }
                        }
                    },
                    {
                        "$match": {
                            "department": department
                        }
                    }
                ]

            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid trend parameters: {str(exc)}"
                )

        pipeline = []
        if endpoint == "employee_monthly":
            first_s, last_s, _, _ = month_bounds(month)
            pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": first_s, "$lte": last_s}}}]
        elif endpoint == "department_summary":
            first_s, last_s, _, _ = month_bounds(month)
            pipeline = [{"$match": {"date": {"$gte": first_s, "$lte": last_s}}}]
        elif endpoint == "late_leaderboard":
            first_s, last_s, _, _ = month_bounds(month)
            pipeline = [{"$match": {"date": {"$gte": first_s, "$lte": last_s}, "late_minutes": {"$gt": 0}}}]
        else:
            start, end = parse_day(from_date), parse_day(to_date)
            if end < start or (end - start).days + 1 > 92:
                raise HTTPException(422, "Invalid trend range")
            pipeline = [{"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}}]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                             verbosity="executionStats")
        collection = "attendance_logs"
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
