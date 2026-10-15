import os
from decimal import Decimal, ROUND_HALF_UP
from pymongo import ReturnDocument, DESCENDING
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}


def epoch_ms(value: Optional[int]) -> Optional[int]:
    if value is None: return None
    if isinstance(value, bool) or not isinstance(value, int) or not 100_000_000_000 <= value <= 4_102_444_800_000: raise ValueError("must be epoch milliseconds")
    return value


def dt_from_ms(value: Optional[int]):
    return None if value is None else datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def ms_from_dt(value):
    if value is None: return None
    if value.tzinfo is None: value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def clean(value):
    if isinstance(value, datetime): return ms_from_dt(value)
    if isinstance(value, dict): return {k: clean(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list): return [clean(v) for v in value]
    return value


def parse_day(value: str) -> date:
    try: return date.fromisoformat(value)
    except ValueError as exc: raise ValueError("must be YYYY-MM-DD") from exc


def parse_month(value: str):
    try:
        year, month = map(int, value.split("-")); first = date(year, month, 1)
        if len(value) != 7: raise ValueError
        return first, date(year, month, monthrange(year, month)[1])
    except (ValueError, TypeError) as exc: raise HTTPException(422, "month must be YYYY-MM") from exc


def minutes(value: str):
    hour, minute = map(int, value.split(":")); return hour * 60 + minute


def attendance_day(moment: datetime, employee: dict):
    local = moment.astimezone(IST)
    if minutes(employee["shift_end"]) <= minutes(employee["shift_start"]) and local.hour * 60 + local.minute < minutes(employee["shift_end"]): local -= timedelta(days=1)
    return local.date().isoformat()


def derived(employee: dict, day: str, punch_in, punch_out):
    if not punch_in: return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    start = datetime.combine(parse_day(day), time.fromisoformat(employee["shift_start"])).replace(tzinfo=IST)
    late = int((punch_in.astimezone(IST) - start).total_seconds() // 60)
    late = late if late > 10 else 0
    if not punch_out: return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    hours = (Decimal(str((punch_out - punch_in).total_seconds())) / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    end = datetime.combine(parse_day(day), time.fromisoformat(employee["shift_end"])).replace(tzinfo=IST)
    if minutes(employee["shift_end"]) <= minutes(employee["shift_start"]): end += timedelta(days=1)
    overtime = int(max(Decimal(0), Decimal(str((punch_out.astimezone(IST) - end).total_seconds()))) // Decimal(60))
    return {"late_minutes": late, "work_hours": float(hours), "overtime_minutes": overtime if overtime >= 30 else 0, "half_day": hours < Decimal("4.50")}


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined(cls, value): parse_day(value); return value

    @model_validator(mode="after")
    def distinct_shift(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_time(cls, value): return epoch_ms(value)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value not in PRESENCE: raise ValueError("invalid presence status")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_time(cls, value): return epoch_ms(value)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_time(cls, value): return epoch_ms(value)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value is not None and value not in STATUSES: raise ValueError("invalid status")
        return value


def record(doc):
    doc = clean(doc)
    for key, value in (("punch_in", None), ("punch_out", None), ("work_hours", None), ("late_minutes", 0), ("overtime_minutes", 0), ("half_day", False), ("history", [])): doc.setdefault(key, value)
    return doc

# --------------------------------------------------------------------------- #
# Punch-Out Helpers (R3, R4, R5)
# --------------------------------------------------------------------------- #
def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    seconds = (punch_out_dt - punch_in_dt).total_seconds()
    hours = Decimal(str(seconds / 3600))
    # ROUND_HALF_UP ensures 4.495 becomes 4.50, unlike standard Python round()
    return float(hours.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

def compute_overtime(punch_out_dt: datetime, shift_start: str, shift_end: str, attendance_date_str: str) -> int:
    """R3: Overtime is whole minutes from shift_end to punch_out, minimum 30."""
    start_h, start_m = parse_time(shift_start)
    end_h, end_m = parse_time(shift_end)
    
    att_date = date.fromisoformat(attendance_date_str)
    shift_end_date = att_date
    
    # If it's an overnight shift, the shift end is on the NEXT calendar day
    if (end_h < start_h) or (end_h == start_h and end_m <= start_m):
        shift_end_date += timedelta(days=1)
        
    shift_end_dt = datetime.combine(shift_end_date, datetime.min.time()).replace(
        hour=end_h, minute=end_m, tzinfo=IST
    )
    
    diff = punch_out_dt - shift_end_dt
    minutes_overtime = int(diff.total_seconds() // 60)
    
    if minutes_overtime >= 30:
        return minutes_overtime
    return 0

# --------------------------------------------------------------------------- #
# Punch-Out Endpoint
# --------------------------------------------------------------------------- #
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)

@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
        
    if body.punched_at:
        ts = ms_to_utc_datetime(body.punched_at)
    else:
        ts = now_utc_truncated()
        
    # Find the most recent record where punch_in is before our punch_out time
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)]
    )
    
    if not record:
        raise HTTPException(status_code=404, detail="No valid punch-in found")
        
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")
        
    # Ensure datetimes are timezone-aware for math
    punch_in_dt = record["punch_in"].replace(tzinfo=timezone.utc)
    ts_aware = ts.replace(tzinfo=timezone.utc)
    
    # Contract constraint checks
    if ts_aware <= punch_in_dt:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
        
    if (ts_aware - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")
        
    # Calculate derived fields
    work_hours = compute_work_hours(punch_in_dt, ts_aware)
    overtime_mins = compute_overtime(ts_aware, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = work_hours < 4.50
    
    # Atomic update: only update if punch_out is STILL null 
    # (prevents a concurrent request from overwriting this)
    updated_doc = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_hours,
            "overtime_minutes": overtime_mins,
            "half_day": half_day
        }},
        return_document=ReturnDocument.AFTER
    )
    
    if not updated_doc:
        raise HTTPException(status_code=409, detail="Record already punched out concurrently")
        
    # Format response for the API boundary
    updated_doc.pop("_id", None)
    updated_doc["punch_in"] = utc_datetime_to_ms(updated_doc["punch_in"])
    updated_doc["punch_out"] = utc_datetime_to_ms(updated_doc["punch_out"])
    
    return updated_doc


def query_dates(date_from, date_to):
    if date_from: parse_day(date_from)
    if date_to: parse_day(date_to)
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must not exceed date_to")


@app.on_event("startup")
def indexes():
    db.employees.create_index("emp_code", unique=True, name="uq_emp_code")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_day")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="attendance_date_emp")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1), ("status", 1)], name="attendance_employee_period")


@app.get("/health")
def health():
    try: client.admin.command("ping")
    except Exception: raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump(); doc["created_at"] = datetime.now(UTC)
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return clean(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department else {}
    items = db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [clean(x) for x in items], "total": db.employees.count_documents(query), "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee: raise HTTPException(404, "employee not found")
    moment = dt_from_ms(body.punched_at) or datetime.now(UTC).replace(microsecond=0); day = attendance_day(moment, employee)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": moment, "punch_out": None, "history": []}; doc.update(derived(employee, day, moment, None))
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "already punched in for this date")
    return record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee: raise HTTPException(404, "employee not found")
    moment = dt_from_ms(body.punched_at) or datetime.now(UTC).replace(microsecond=0)
    candidate = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": moment}, "punch_out": None}, sort=[("punch_in", -1)])
    if not candidate: raise HTTPException(404, "no open punch-in found")
    if (moment - candidate["punch_in"]).total_seconds() <= 0 or (moment - candidate["punch_in"]).total_seconds() > 86400: raise HTTPException(422, "invalid punch-out")
    values = derived(employee, candidate["date"], candidate["punch_in"], moment)
    updated = db.attendance_logs.find_one_and_update({"_id": candidate["_id"], "punch_out": None}, {"$set": {"punch_out": moment, **values}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "record is already punched out")
    return record(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query_dates(date_from, date_to); query = {}
    if emp_code: query["emp_code"] = emp_code
    if status: query["status"] = status
    if date_from or date_to: query["date"] = {**({"$gte": date_from} if date_from else {}), **({"$lte": date_to} if date_to else {})}
    total = db.attendance_logs.count_documents(query); cursor = db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record(x) for x in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{day}")
def regularize(emp_code: str, day: str, body: RegularizeIn):
    parse_day(day); employee = db.employees.find_one({"emp_code": emp_code}); current = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if not employee or not current: raise HTTPException(404, "employee or attendance record not found")
    old = record(current); status = body.status if body.status is not None else old["status"]
    if status in {"ABSENT", "LEAVE"} and (body.punch_in is not None or body.punch_out is not None): raise HTTPException(422, "absence and leave cannot have punch times")
    pin = dt_from_ms(body.punch_in) if body.punch_in is not None else old["punch_in"]; pout = dt_from_ms(body.punch_out) if body.punch_out is not None else old["punch_out"]
    if status in {"ABSENT", "LEAVE"}: pin = pout = None
    if status in PRESENCE and pin is None: raise HTTPException(422, "presence status requires punch_in")
    if pin and attendance_day(pin, employee) != day: raise HTTPException(422, "punch_in must belong to record date")
    if pout and (not pin or pout <= pin or (pout - pin).total_seconds() > 86400): raise HTTPException(422, "invalid punch_out")
    values = {"status": status, "punch_in": pin, "punch_out": pout, **derived(employee, day, pin, pout)}; changes = {}
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if old.get(key) != clean(values[key]): changes[key] = {"from": old.get(key), "to": values[key]}
    if not changes: raise HTTPException(422, "request changes nothing")
    history = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update({"_id": current["_id"], "history": current.get("history", [])}, {"$set": values, "$push": {"history": history}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "concurrent correction")
    return record(updated)


def period_records(match): return list(db.attendance_logs.aggregate([{"$match": match}, {"$sort": {"date": 1, "emp_code": 1}}]))
def present_value(doc): return 0 if doc.get("status") not in PRESENCE else (0.5 if doc.get("half_day", False) else 1)
def rounded(value, places=2): return float(Decimal(str(value)).quantize(Decimal("1." + "0" * places), rounding=ROUND_HALF_UP))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    # 1. Fetch employee & validate month bounds
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "Employee not found")
        
    first_day, last_day = parse_month(month)
    first_day_str = first_day.isoformat()
    last_day_str = last_day.isoformat()
    
    # 2. Calculate working days (O(1) calendar math, no database looping)
    start_date = max(first_day, date.fromisoformat(employee["joined_on"]))
    working_days = 0
    if start_date <= last_day:
        days_in_range = (last_day - start_date).days + 1
        working_days = sum(1 for i in range(days_in_range) if (start_date + timedelta(days=i)).weekday() < 5)
        
    # 3. MongoDB Aggregation Pipeline
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day_str, "$lte": last_day_str}
            }
        },
        {
            "$addFields": {
                # MongoDB $dayOfWeek: 1=Sun, 2=Mon, 3=Tue, 4=Wed, 5=Thu, 6=Fri, 7=Sat
                "is_weekday": {
                    "$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            # Only count present days if status matches AND it is a weekday
                            {"$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                "$is_weekday"
                            ]},
                            {"$cond": ["$half_day", 0.5, 1]},
                            0
                        ]
                    }
                }
            }
        }
    ]
    
    result = list(db.attendance_logs.aggregate(pipeline))
    
    # Handle employees with zero logs in the requested month
    stats = result[0] if result else {
        "present_days": 0.0, "leave_days": 0, "late_count": 0, 
        "total_late_minutes": 0, "total_overtime_minutes": 0
    }
    
    # 4. Calculate percentage with strict half-up rounding (R8)
    attendance_pct = None
    if working_days > 0:
        pct = (Decimal(str(stats["present_days"])) / Decimal(working_days)) * Decimal(100)
        attendance_pct = float(pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(stats["present_days"]),
        "leave_days": stats["leave_days"],
        "late_count": stats["late_count"],
        "total_late_minutes": stats["total_late_minutes"],
        "total_overtime_minutes": stats["total_overtime_minutes"],
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first_day, last_day = parse_month(month)
    first_day_str = first_day.isoformat()
    last_day_str = last_day.isoformat()

    # Base match for employees who joined on or before the last day of the month (R9)
    emp_match = {"joined_on": {"$lte": last_day_str}}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        
        # Join attendance logs for this specific month
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp_code": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$and": [
                        {"$eq": ["$emp_code", "$$emp_code"]},
                        {"$gte": ["$date", first_day_str]},
                        {"$lte": ["$date", last_day_str]}
                    ]}
                }},
                # Determine if the log falls on a weekday (2=Mon ... 6=Fri)
                {"$addFields": {
                    "is_weekday": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
                }}
            ],
            "as": "logs"
        }},
        
        # Unwind logs, preserving empty arrays so employees with 0 logs still count for headcount
        {"$unwind": {
            "path": "$logs",
            "preserveNullAndEmptyArrays": True
        }},
        
        # Group by department and sum all the required fields
        {"$group": {
            "_id": "$department",
            "unique_emps": {"$addToSet": "$emp_code"},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$eq": ["$logs.is_weekday", True]}
                        ]},
                        {"$cond": ["$logs.half_day", 0.5, 1]},
                        0
                    ]
                }
            },
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            
            # To calculate the average work hours over records, we sum them and count them
            "sum_work_hours": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$logs.work_hours", None]}
                        ]},
                        "$logs.work_hours",
                        0
                    ]
                }
            },
            "count_work_hours": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$ne": ["$logs.work_hours", None]}
                        ]},
                        1,
                        0
                    ]
                }
            }
        }},
        
        # Sort alphabetically by department as required
        {"$sort": {"_id": 1}}
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    
    for res in results:
        # Calculate precise half-up rounding for average work hours (R8)
        avg_work_hours = None
        if res["count_work_hours"] > 0:
            avg = Decimal(str(res["sum_work_hours"])) / Decimal(str(res["count_work_hours"]))
            avg_work_hours = float(avg.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            
        items.append({
            "department": res["_id"],
            "headcount": len(res["unique_emps"]),
            "present_days": float(res["present_days"]),
            "avg_work_hours": avg_work_hours,
            "late_count": res["late_count"],
            "total_late_minutes": res["total_late_minutes"],
            "leave_count": res["leave_count"],
            "on_duty_count": res["on_duty_count"]
        })

    return {"month": month, "items": items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first_day, last_day = parse_month(month)
    
    pipeline = [
        # 1. Match attendance logs for the month with late_minutes > 0
        {
            "$match": {
                "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()},
                "late_minutes": {"$gt": 0}
            }
        },
        # 2. Group by employee to sum late minutes and count
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        # 3. Lookup employee details across collections
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp_info"
            }
        },
        # 4. Unwind (inner join: ignores logs for unknown employees)
        {"$unwind": "$emp_info"},
        
        # 5. Format fields
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp_info.name",
                "department": "$emp_info.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ]
    
    # Apply department filter if provided
    if department:
        pipeline.append({"$match": {"department": department}})
        
    pipeline.extend([
        # 6. Apply Window Function for standard competition ranking
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        # 7. Apply limit AFTER ranking (allows ties at the cutoff)
        {"$match": {"rank": {"$lte": limit}}},
        
        # 8. Final tie-breaker sort: total_late_minutes DESC, emp_code ASC
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ])
    
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query(...)):
    start_date, end_date = parse_day(from_), parse_day(to)
    if end_date < start_date:
        raise HTTPException(422, "to must not precede from")
    if (end_date - start_date).days > 91:
        raise HTTPException(422, "range exceeds 92 days")
        
    # Get the department's employees to calculate headcount and filter logs
    employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not employees:
        raise HTTPException(404, "department not found")
        
    emp_codes = [e["emp_code"] for e in employees]
    
    # We aggregate on the 'employees' collection because we need a guaranteed starting document 
    # to trigger the $densify gap-filling sequence, even if there are zero attendance logs.
    pipeline = [
        {"$limit": 1},
        {"$project": {"_id": 0}},
        
        # 1. Generate exactly one document per calendar day in the range
        {"$densify": {
            "field": "date_dt",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [
                    datetime.combine(start_date, time.min), 
                    datetime.combine(end_date + timedelta(days=1), time.min)
                ]
            }
        }},
        
        # 2. Format the date and determine if it is a working day (Mon-Fri)
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
            "is_working_day": {"$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]},
            "employees_list": employees
        }},
        
        # 3. Calculate headcount: count employees who joined on or before this specific day (R9)
        {"$addFields": {
            "headcount": {
                "$size": {
                    "$filter": {
                        "input": "$employees_list",
                        "as": "emp",
                        "cond": {"$lte": ["$$emp.joined_on", "$date"]}
                    }
                }
            }
        }},
        
        # 4. Lookup the daily logs for the department's employees on this specific day
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"current_date": "$date"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$and": [
                        {"$in": ["$emp_code", emp_codes]},
                        {"$eq": ["$date", "$$current_date"]}
                    ]}
                }}
            ],
            "as": "daily_logs"
        }},
        
        # 5. Process the daily stats (present count handles half days = 0.5)
        {"$addFields": {
            "present_count": {
                "$sum": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$daily_logs",
                                "as": "log",
                                "cond": {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]}
                            }
                        },
                        "as": "plog",
                        "in": {"$cond": ["$$plog.half_day", 0.5, 1]}
                    }
                }
            },
            "late_count": {
                "$size": {
                    "$filter": {
                        "input": "$daily_logs",
                        "as": "log",
                        "cond": {"$gt": ["$$log.late_minutes", 0]}
                    }
                }
            }
        }},
        
        # 6. Calculate daily attendance rate (null on weekends or if headcount is 0)
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                    None
                ]
            }
        }},
        
        # 7. Apply standard Window Function for the 7-day moving average
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]}
                }
            }
        }},
        
        # 8. Final formatting and cleanup
        {"$project": {
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]},
            "_id": 0
        }}
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str, 
    page: int = Query(1, ge=1), 
    page_size: int = Query(20, ge=1, le=100), 
    emp_code: Optional[str] = None, 
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None, 
    date_to: Optional[str] = None, 
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None
):
    """
    Runs MongoDB's explain with executionStats on the main query for the requested endpoint.
    """
    explanation = None
    collection_name = ""

    if endpoint == "attendance_list":
        query_dates(date_from, date_to)
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if status: q["status"] = status
        if date_from or date_to: 
            q["date"] = {**({"$gte": date_from} if date_from else {}), **({"$lte": date_to} if date_to else {})}
        
        collection_name = "attendance_logs"
        explanation = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size).explain("executionStats")

    elif endpoint == "employee_monthly":
        if not emp_code or not month: raise HTTPException(422, "emp_code and month required")
        first_day, last_day = parse_month(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}}}]
        collection_name = "attendance_logs"
        explanation = db.command("explain", {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    elif endpoint == "department_summary":
        if not month: raise HTTPException(422, "month required")
        first_day, last_day = parse_month(month)
        pipeline = [{"$match": {"joined_on": {"$lte": last_day.isoformat()}}}]
        if department: pipeline[0]["$match"]["department"] = department
        collection_name = "employees"
        explanation = db.command("explain", {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    elif endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month required")
        first_day, last_day = parse_month(month)
        pipeline = [{"$match": {"date": {"$gte": first_day.isoformat(), "$lte": last_day.isoformat()}, "late_minutes": {"$gt": 0}}}]
        collection_name = "attendance_logs"
        explanation = db.command("explain", {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    elif endpoint == "department_trend":
        if not department or not from_ or not to: raise HTTPException(422, "department, from, and to required")
        pipeline = [{"$limit": 1}]
        collection_name = "employees"
        explanation = db.command("explain", {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    else:
        raise HTTPException(422, "unknown endpoint")

    return {"endpoint": endpoint, "collection": collection_name, "explain": clean(explanation)}