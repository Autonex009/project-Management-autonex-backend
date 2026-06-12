"""
Payroll Calculation API
-----------------------
GET  /api/payroll/preview?month=YYYY-MM      — compute salary for all employees (no save)
POST /api/payroll/save                        — save / finalize a payroll run
GET  /api/payroll/saved?month=YYYY-MM        — retrieve a saved run with final numbers
PATCH /api/employees/{id}/salary             — update employee base salary

All endpoints are admin-only (checked by role in request context via query param for now;
caller must pass current_user_id which maps to a user with role=admin).
"""
import io
import csv
import os
import hmac
import hashlib
from calendar import monthrange
from datetime import date as date_type, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.employee import Employee
from app.models.leave import Leave
from app.models.payroll import PayrollLeaveAdjustment, PayrollRun
from app.models.user import User
from app.constants.leave_types import (
    is_non_working_day,
    normalize_leave_type,
    get_annual_leave_quota,
    ANNUAL_LEAVE_QUOTA,
    INTERN_MONTHLY_PAID_QUOTA,
    is_intern,
    get_leave_type_label,
)
from app.services.salary_crypto import decrypt_salary


def require_payroll_passcode(
    x_payroll_passcode: Optional[str] = Header(default=None),
    passcode: Optional[str] = Query(default=None),  # query fallback for the CSV download link
):
    """Gate every payroll endpoint behind a shared passcode.

    Only the SHA-256 HASH of the passcode is stored (in the PAYROLL_PASSCODE_HASH
    env var, a sensitive secret on the production deployment) — never the passcode
    itself, so the stored value can't be reversed. Requests supply the plaintext
    passcode via the X-Payroll-Passcode header (or ?passcode= for downloads); the
    server hashes it and compares in constant time. If the hash env var is unset,
    the gate is DISABLED (dev convenience — local DBs hold no real salaries).
    """
    expected_hash = (os.getenv("PAYROLL_PASSCODE_HASH") or "").strip().lower()
    if not expected_hash:
        return
    provided = x_payroll_passcode or passcode or ""
    provided_hash = hashlib.sha256(provided.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(provided_hash, expected_hash):
        raise HTTPException(status_code=401, detail="Invalid or missing payroll passcode")


# Passcode dependency applies to ALL routes on this router.
router = APIRouter(
    prefix="/api/payroll",
    tags=["payroll"],
    dependencies=[Depends(require_payroll_passcode)],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _working_days_in_month(month_start: date_type, month_end: date_type) -> int:
    """Count working days in the month — excludes weekends and fixed holidays."""
    count = 0
    d = month_start
    while d <= month_end:
        if not is_non_working_day(d):
            count += 1
        d += timedelta(days=1)
    return count


def _working_days_in_month_overlap(start: date_type, end: date_type, month_start: date_type, month_end: date_type) -> int:
    """Count working days (excl. weekends & fixed holidays) a leave overlaps within the month."""
    effective_start = max(start, month_start)
    effective_end = min(end, month_end)
    count = 0
    d = effective_start
    while d <= effective_end:
        if not is_non_working_day(d):
            count += 1
        d += timedelta(days=1)
    return count


def _working_dates(start: date_type, end: date_type, year: int):
    """Yield each working date (excl. weekends & fixed holidays) in [start, end] within `year`."""
    d = start
    while d <= end:
        if d.year == year and not is_non_working_day(d):
            yield d
        d += timedelta(days=1)


def _classify_year_leaves(leaves: list, year: int, intern: bool = False, intern_until: date_type | None = None):
    """
    Apply the paid-leave entitlement to one employee's approved leaves for a calendar year.

    Walks leaves chronologically; each working day consumes its leave type's quota.
    Days within quota are PAID (no salary impact); days beyond it are UNPAID (deducted).
    A single leave may straddle the boundary (part paid, part unpaid).

    Entitlement differs by employee type:
      • Employees: annual quota per leave type (ANNUAL_LEAVE_QUOTA), consumed over the year.
      • Interns:   PAID leave accrues MONTHLY (INTERN_MONTHLY_PAID_QUOTA per calendar month,
                   resetting each month); casual_sick / floater keep the same annual quotas.

    `intern_until` preserves history across an intern→full-time promotion: when set,
    PAID days BEFORE that date use the monthly intern rule and days ON/AFTER it use the
    annual quota — so a promotion never retroactively reclassifies leave already taken.

    Returns:
      classification: {leave_id: {"paid_dates": set, "unpaid_dates": set, "type": str}}
      balances:       {leave_type: {"quota": int, "used": int, "remaining": int, "period": str}}
                      For interns the "paid" balance is monthly; preview_payroll scopes it to
                      the month being run.
    """
    used: dict[str, int] = {}                       # annual usage (employees; intern non-paid)
    used_paid_by_month: dict[tuple, int] = {}       # (year, month) -> intern paid days used
    classification: dict[int, dict] = {}

    for leave in sorted(leaves, key=lambda lv: (lv.start_date, lv.id)):
        ltype = normalize_leave_type(leave.leave_type)
        paid_dates, unpaid_dates = set(), set()
        for wd in _working_dates(leave.start_date, leave.end_date, year):
            use_monthly = ltype == "paid" and (intern or (intern_until is not None and wd < intern_until))
            if use_monthly:
                # Monthly entitlement that resets each calendar month.
                key = (wd.year, wd.month)
                used_paid_by_month[key] = used_paid_by_month.get(key, 0) + 1
                if used_paid_by_month[key] <= INTERN_MONTHLY_PAID_QUOTA:
                    paid_dates.add(wd)
                else:
                    unpaid_dates.add(wd)
            else:
                quota = get_annual_leave_quota(ltype)
                used[ltype] = used.get(ltype, 0) + 1
                if used[ltype] <= quota:
                    paid_dates.add(wd)
                else:
                    unpaid_dates.add(wd)
        classification[leave.id] = {"paid_dates": paid_dates, "unpaid_dates": unpaid_dates, "type": ltype}

    balances = {}
    for ltype, quota in ANNUAL_LEAVE_QUOTA.items():
        if intern and ltype == "paid":
            # Monthly entitlement — month-scoped figures are filled in by preview_payroll.
            balances[ltype] = {
                "quota": INTERN_MONTHLY_PAID_QUOTA,
                "used": 0,
                "remaining": INTERN_MONTHLY_PAID_QUOTA,
                "period": "month",
            }
            continue
        used_days = used.get(ltype, 0)
        balances[ltype] = {
            "quota": quota,
            "used": used_days,
            "remaining": max(quota - used_days, 0),
            "period": "year",
        }
    return classification, balances


def _month_bounds(month: str):
    """Return (month_start, month_end_inclusive) for a YYYY-MM string."""
    try:
        year, mo = int(month[:4]), int(month[5:7])
    except Exception:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")
    last_day = monthrange(year, mo)[1]
    return date_type(year, mo, 1), date_type(year, mo, last_day)


def _build_employee_row(emp: Employee, approved_leaves: list, working_days: int,
                        balances: dict, saved_adjustments: dict = None) -> dict:
    """
    Each leave in `approved_leaves` carries `auto_unpaid_days` (from the annual-quota
    classifier) and `days_in_month` (working days within the month).

    saved_adjustments: {leave_id: {"deduct": bool, "unpaid_days": int|None}} — a finalized
    run's snapshot / admin override. When present it wins over the auto classification.
    When None (live preview), the auto classification is used.
    """
    base = decrypt_salary(emp.base_salary_enc) or 0.0
    per_day = round(base / working_days, 2) if working_days > 0 else 0.0

    leave_rows = []
    total_deducted_days = 0

    for leave in approved_leaves:
        leave_id = leave["leave_id"]
        days = leave["days_in_month"]
        auto_unpaid = leave.get("auto_unpaid_days", 0)

        snap = saved_adjustments.get(leave_id) if saved_adjustments is not None else None
        if snap is not None:
            if snap.get("unpaid_days") is not None:
                unpaid_days = max(0, min(int(snap["unpaid_days"]), days))
            else:  # legacy boolean-only row
                unpaid_days = days if snap.get("deduct", True) else 0
            source = "manual"
        else:
            unpaid_days = auto_unpaid
            source = "auto"

        paid_days = days - unpaid_days
        deduct = unpaid_days > 0
        deduction_amount = round(unpaid_days * per_day, 2)
        if unpaid_days >= days and days > 0:
            classification = "unpaid"
        elif unpaid_days == 0:
            classification = "paid"
        else:
            classification = "partial"
        total_deducted_days += unpaid_days

        leave_rows.append({
            **leave,
            "deduct": deduct,
            "paid_days": paid_days,
            "unpaid_days": unpaid_days,
            "classification": classification,
            "source": source,
            "deduction_amount": deduction_amount,
        })

    total_deduction = round(total_deducted_days * per_day, 2)
    payable_days = working_days - total_deducted_days
    final_salary = round(max(base - total_deduction, 0), 2)

    return {
        "employee_id": emp.id,
        "employee_name": emp.name,
        "designation": emp.designation,
        "employee_type": emp.employee_type,
        "base_salary": base,
        "working_days": working_days,
        "per_day_rate": per_day,
        "leaves": leave_rows,
        "leave_balances": balances,
        "total_leave_days": sum(l["days_in_month"] for l in approved_leaves),
        "total_paid_days": sum(l["paid_days"] for l in leave_rows),
        "total_deducted_days": total_deducted_days,
        "total_deduction": total_deduction,
        "payable_days": payable_days,
        "final_salary": final_salary,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/preview")
def preview_payroll(
    month: str = Query(..., description="YYYY-MM"),
    db: Session = Depends(get_db),
):
    """
    Compute salary for all active employees for the given month.
    Approved leaves default to deduct=True.
    Employees without a base_salary set are included but flagged.
    """
    month_start, month_end = _month_bounds(month)
    working_days = _working_days_in_month(month_start, month_end)
    year = month_start.year
    year_start, year_end = date_type(year, 1, 1), date_type(year, 12, 31)

    employees = db.query(Employee).filter(Employee.status == "active").order_by(Employee.name).all()

    # Fetch the whole CALENDAR YEAR of approved leaves so the annual paid-leave
    # entitlement can be applied chronologically (a leave's paid/unpaid split
    # depends on how much of the year's quota was already consumed before it).
    year_leaves = db.query(Leave).filter(
        Leave.status == "approved",
        Leave.start_date <= year_end,
        Leave.end_date >= year_start,
    ).all()

    leaves_by_emp = {}
    for leave in year_leaves:
        leaves_by_emp.setdefault(leave.employee_id, []).append(leave)

    # Check if a finalized run exists for this month
    existing_run = db.query(PayrollRun).filter(PayrollRun.month == month).first()
    saved_adjustments = {}
    if existing_run:
        adjs = db.query(PayrollLeaveAdjustment).filter(
            PayrollLeaveAdjustment.payroll_run_id == existing_run.id
        ).all()
        saved_adjustments = {a.leave_id: {"deduct": a.deduct, "unpaid_days": a.unpaid_days} for a in adjs}

    rows = []
    for emp in employees:
        emp_year_leaves = leaves_by_emp.get(emp.id, [])
        emp_is_intern = is_intern(emp.employee_type)
        # A promoted intern keeps the monthly rule for leave taken before the promotion.
        intern_until = emp.converted_to_fulltime_at.date() if emp.converted_to_fulltime_at else None
        classification, balances = _classify_year_leaves(emp_year_leaves, year, emp_is_intern, intern_until)

        # Interns' paid leave is monthly — report the balance for THIS month.
        if emp_is_intern and "paid" in balances:
            paid_used_month = sum(
                1
                for cls in classification.values()
                if cls["type"] == "paid"
                for d in cls["paid_dates"]
                if month_start <= d <= month_end
            )
            balances["paid"] = {
                "quota": INTERN_MONTHLY_PAID_QUOTA,
                "used": paid_used_month,
                "remaining": max(INTERN_MONTHLY_PAID_QUOTA - paid_used_month, 0),
                "period": "month",
            }

        # Build rows only for leaves that touch THIS month, scoping the auto
        # paid/unpaid day counts to the month being run.
        month_leaves = []
        for leave in sorted(emp_year_leaves, key=lambda lv: (lv.start_date, lv.id)):
            if leave.start_date > month_end or leave.end_date < month_start:
                continue
            cls = classification.get(leave.id, {"paid_dates": set(), "unpaid_dates": set()})
            paid_in_month = sum(1 for d in cls["paid_dates"] if month_start <= d <= month_end)
            unpaid_in_month = sum(1 for d in cls["unpaid_dates"] if month_start <= d <= month_end)
            days_in_month = paid_in_month + unpaid_in_month
            if days_in_month <= 0:
                continue
            month_leaves.append({
                "leave_id": leave.id,
                "leave_type": leave.leave_type,
                "leave_type_label": get_leave_type_label(leave.leave_type),
                "start_date": leave.start_date.isoformat(),
                "end_date": leave.end_date.isoformat(),
                "days_in_month": days_in_month,
                "auto_unpaid_days": unpaid_in_month,
                "reason": leave.reason or "",
            })

        row = _build_employee_row(
            emp, month_leaves, working_days, balances,
            saved_adjustments if existing_run else None,
        )
        row["salary_missing"] = decrypt_salary(emp.base_salary_enc) is None
        rows.append(row)

    return {
        "month": month,
        "working_days": working_days,
        "annual_leave_quota": ANNUAL_LEAVE_QUOTA,
        "run_status": existing_run.status if existing_run else None,
        "run_id": existing_run.id if existing_run else None,
        "employees": rows,
    }


class LeaveAdjustmentIn(BaseModel):
    employee_id: int
    leave_id: int
    deduct: bool
    unpaid_days: Optional[int] = None   # snapshot of unpaid working-days; preserves partial classifications


class SavePayrollBody(BaseModel):
    month: str
    status: str = "draft"          # "draft" or "finalized"
    notes: Optional[str] = None
    adjustments: List[LeaveAdjustmentIn]
    processed_by: Optional[int] = None


@router.post("/save")
def save_payroll(body: SavePayrollBody, db: Session = Depends(get_db)):
    """
    Upsert a payroll run and its leave adjustments.
    Calling with status='finalized' locks the run.
    """
    if body.status not in ("draft", "finalized"):
        raise HTTPException(status_code=422, detail="status must be 'draft' or 'finalized'")

    month_start, month_end = _month_bounds(body.month)
    working_days = _working_days_in_month(month_start, month_end)

    run = db.query(PayrollRun).filter(PayrollRun.month == body.month).first()
    if run:
        if run.status == "finalized" and body.status != "finalized":
            raise HTTPException(status_code=400, detail="Payroll already finalized for this month")
        run.status = body.status
        run.working_days = working_days
        run.notes = body.notes
        run.processed_by = body.processed_by
        # Delete existing adjustments and re-insert
        db.query(PayrollLeaveAdjustment).filter(
            PayrollLeaveAdjustment.payroll_run_id == run.id
        ).delete()
    else:
        run = PayrollRun(
            month=body.month,
            status=body.status,
            working_days=working_days,
            notes=body.notes,
            processed_by=body.processed_by,
        )
        db.add(run)
        db.flush()

    for adj in body.adjustments:
        db.add(PayrollLeaveAdjustment(
            payroll_run_id=run.id,
            employee_id=adj.employee_id,
            leave_id=adj.leave_id,
            deduct=adj.deduct,
            unpaid_days=adj.unpaid_days,
        ))

    db.commit()
    db.refresh(run)
    return {"message": f"Payroll {body.status} for {body.month}", "run_id": run.id, "status": run.status}


@router.get("/saved")
def get_saved_payroll(
    month: str = Query(..., description="YYYY-MM"),
    db: Session = Depends(get_db),
):
    """Retrieve a saved/finalized payroll run with full calculations."""
    run = db.query(PayrollRun).filter(PayrollRun.month == month).first()
    if not run:
        raise HTTPException(status_code=404, detail="No payroll run found for this month")

    # Reuse preview logic with saved adjustments
    return preview_payroll(month=month, db=db)


@router.get("/export.csv")
def export_payroll_csv(
    month: str = Query(..., description="YYYY-MM"),
    db: Session = Depends(get_db),
):
    """Download the payroll summary as a CSV file."""
    data = preview_payroll(month=month, db=db)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Employee", "Designation", "Type",
        "Base Salary (₹)", f"Per Day (₹, ÷{data['working_days']})",
        "Leave Days", "Paid Days", "Unpaid (Deducted) Days", "Deduction (₹)", "Final Salary (₹)", "Notes"
    ])
    for row in data["employees"]:
        writer.writerow([
            row["employee_name"],
            row["designation"] or "",
            row["employee_type"],
            row["base_salary"],
            row["per_day_rate"],
            row["total_leave_days"],
            row.get("total_paid_days", 0),
            row["total_deducted_days"],
            row["total_deduction"],
            row["final_salary"],
            "Salary not set" if row.get("salary_missing") else "",
        ])

    output.seek(0)
    filename = f"payroll_{month}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
