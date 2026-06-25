"""
Payroll Calculation API
-----------------------
GET  /api/payroll/preview?month=YYYY-MM      — compute salary for all employees (no save)
POST /api/payroll/save                        — save / finalize a payroll run
GET  /api/payroll/saved?month=YYYY-MM        — retrieve a saved run with final numbers
PATCH /api/employees/{id}/salary             — update employee base salary

Pay (ground-truth salary records) — the source data the Monthly Pay calc derives from:
GET  /api/payroll/salaries                    — list employees with their base monthly salary
PUT  /api/payroll/salaries/{employee_id}      — set an employee's base monthly salary

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
from app.models.payroll import PayrollLeaveAdjustment, PayrollRun, PayrollBonus, PayrollAdditionalPayment, Salary
from app.models.user import User
from app.constants.leave_types import (
    is_non_working_day,
    normalize_leave_type,
    get_annual_leave_quota,
    ANNUAL_LEAVE_QUOTA,
    INTERN_MONTHLY_PAID_QUOTA,
    is_intern,
    is_intern_or_contractor,
    get_leave_type_label,
)
from app.services.salary_crypto import decrypt_salary, encrypt_salary, encryption_enabled


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
      classification: {leave_id: {"paid_dates": dict, "unpaid_dates": dict, "type": str}}
      balances:       {leave_type: {"quota": float, "used": float, "remaining": float, "period": str}}
                      For interns the "paid" balance is monthly; preview_payroll scopes it to
                      the month being run.
    """
    used: dict[str, float] = {}                       # annual usage (employees; intern non-paid)
    used_paid_by_month: dict[tuple, float] = {}       # (year, month) -> intern paid days used
    classification: dict[int, dict] = {}

    for leave in sorted(leaves, key=lambda lv: (lv.start_date or date_type.min, lv.id)):
        ltype = normalize_leave_type(leave.leave_type)
        paid_dates, unpaid_dates = {}, {}
        
        # Bypass branch for sheet-synced placeholder leaves with NULL dates
        if leave.start_date is None or leave.end_date is None:
            duration = 0.5 if getattr(leave, "is_half_day", False) else 1.0
            used[ltype] = used.get(ltype, 0.0) + duration
            classification[leave.id] = {"paid_dates": {}, "unpaid_dates": {}, "type": ltype}
            continue

        weight = 0.5 if getattr(leave, "is_half_day", False) else 1.0
        for wd in _working_dates(leave.start_date, leave.end_date, year):
            use_monthly = ltype == "paid" and (intern or (intern_until is not None and wd < intern_until))
            if use_monthly:
                # Monthly entitlement that resets each calendar month.
                key = (wd.year, wd.month)
                used_paid_by_month[key] = used_paid_by_month.get(key, 0.0) + weight
                excess = used_paid_by_month[key] - INTERN_MONTHLY_PAID_QUOTA
                if excess <= 0:
                    paid_dates[wd] = weight
                elif excess < weight:
                    paid_dates[wd] = weight - excess
                    unpaid_dates[wd] = excess
                else:
                    unpaid_dates[wd] = weight
            else:
                is_intern_for_date = intern or (intern_until is not None and wd < intern_until)
                if is_intern_for_date and ltype == "casual_sick":
                    unpaid_dates[wd] = weight
                else:
                    quota = get_annual_leave_quota(ltype)
                    used[ltype] = used.get(ltype, 0.0) + weight
                    excess = used[ltype] - quota
                    if excess <= 0:
                        paid_dates[wd] = weight
                    elif excess < weight:
                        paid_dates[wd] = weight - excess
                        unpaid_dates[wd] = excess
                    else:
                        unpaid_dates[wd] = weight
        classification[leave.id] = {"paid_dates": paid_dates, "unpaid_dates": unpaid_dates, "type": ltype}

    balances = {}
    for ltype, quota in ANNUAL_LEAVE_QUOTA.items():
        if intern and ltype == "paid":
            # Monthly entitlement — month-scoped figures are filled in by preview_payroll.
            balances[ltype] = {
                "quota": INTERN_MONTHLY_PAID_QUOTA,
                "used": 0.0,
                "remaining": INTERN_MONTHLY_PAID_QUOTA,
                "period": "month",
            }
            continue

        if intern and ltype == "casual_sick":
            quota = 0.0

        used_days = used.get(ltype, 0.0)
        balances[ltype] = {
            "quota": quota,
            "used": used_days,
            "remaining": max(quota - used_days, 0.0),
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


def _normalize_name(name: Optional[str]) -> str:
    """Lower-case, collapse whitespace — used to match employees to salary rows by name."""
    return " ".join((name or "").lower().split())


def _parse_money(text: Optional[str]) -> Optional[float]:
    """Parse a stored pay string like '₹100,000' into a float. None/empty → None."""
    if text is None:
        return None
    cleaned = "".join(ch for ch in str(text) if ch.isdigit() or ch == ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _format_money(amount: Optional[float]) -> Optional[str]:
    """Format a number back into the stored '₹100,000' text form. None → None."""
    if amount is None:
        return None
    return f"₹{int(round(amount)):,}"


def _read_pay(stored: Optional[str]) -> Optional[float]:
    """Read a salary-table pay value as a float, transparently handling encryption.

    Values are encrypted at rest (Fernet, keyed by SALARY_KEY) so a raw DB SELECT
    only ever shows ciphertext. We first try to decrypt; if that fails we fall back
    to parsing legacy plaintext like '₹100,000' — this keeps reads working both
    before and during the one-time migration. With no key configured, encrypted
    values can't be read and return None (intentional).
    """
    if not stored:
        return None
    decrypted = decrypt_salary(stored)
    if decrypted is not None:
        return decrypted
    return _parse_money(stored)


def _build_employee_row(emp: Employee, approved_leaves: list, working_days: int,
                        balances: dict, saved_adjustments: dict = None,
                        base_salary: Optional[float] = None,
                        bonus_limit: float = 0.0, bonus: float = 0.0,
                        additional_payment: float = 0.0) -> dict:
    """
    Each leave in `approved_leaves` carries `auto_unpaid_days` (from the annual-quota
    classifier) and `days_in_month` (working days within the month).

    saved_adjustments: {leave_id: {"deduct": bool, "unpaid_days": int|None}} — a finalized
    run's snapshot / admin override. When present it wins over the auto classification.
    When None (live preview), the auto classification is used.

    base_salary: the monthly base pay to use for this employee. Passed in from the
    `salary` table (the Pay tab's source of truth). None means no salary on record.
    """
    base = base_salary or 0.0
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
    # Bonus is discretionary and capped at the employee's limit; additional
    # payments are free-form. Both add on top of the post-deduction salary.
    bonus_amount = round(max(0.0, min(bonus or 0.0, bonus_limit or 0.0)), 2)
    additional_amount = round(max(0.0, additional_payment or 0.0), 2)
    final_salary = round(max(base - total_deduction, 0) + bonus_amount + additional_amount, 2)

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
        "bonus_limit": round(bonus_limit or 0.0, 2),
        "bonus": bonus_amount,
        "additional_payment": additional_amount,
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

    # Pay now comes from the `salary` table (the Pay tab's source of truth), matched
    # to each employee by name. Rows marked Inactive there are excluded from the run.
    salary_by_name = {}
    for s in db.query(Salary).all():
        salary_by_name[_normalize_name(s.full_name)] = s

    # Fetch the whole CALENDAR YEAR of approved leaves (including NULL dates placeholders)
    # so the annual paid-leave entitlement can be applied chronologically.
    year_leaves = db.query(Leave).filter(
        Leave.status == "approved",
        (
            ((Leave.start_date <= year_end) & (Leave.end_date >= year_start)) |
            Leave.start_date.is_(None)
        )
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

    # Discretionary bonuses persisted on this run (employee_id -> amount).
    saved_bonuses = {}
    saved_additional = {}
    if existing_run:
        saved_bonuses = {
            b.employee_id: b.amount
            for b in db.query(PayrollBonus).filter(PayrollBonus.payroll_run_id == existing_run.id).all()
        }
        saved_additional = {
            a.employee_id: a.amount
            for a in db.query(PayrollAdditionalPayment).filter(PayrollAdditionalPayment.payroll_run_id == existing_run.id).all()
        }

    rows = []
    for emp in employees:
        # Resolve this employee's pay from the salary table (by name).
        salary_row = salary_by_name.get(_normalize_name(emp.name))
        # Skip anyone explicitly marked Inactive in the salary table.
        if salary_row is not None and (salary_row.status or "").strip().lower() == "inactive":
            continue
        emp_base_salary = _read_pay(salary_row.base_pay_monthly) if salary_row is not None else decrypt_salary(emp.base_salary_enc)
        # Bonus cap comes from the salary table; the granted amount (if any) is saved on the run.
        emp_bonus_limit = (_read_pay(salary_row.opt_bonus_monthly) if salary_row is not None else None) or 0.0
        emp_bonus = saved_bonuses.get(emp.id, 0.0)
        emp_additional = saved_additional.get(emp.id, 0.0)

        emp_year_leaves = leaves_by_emp.get(emp.id, [])
        emp_is_intern = is_intern_or_contractor(emp.employee_type)
        # A promoted intern keeps the monthly rule for leave taken before the promotion.
        intern_until = emp.converted_to_fulltime_at.date() if emp.converted_to_fulltime_at else None
        classification, balances = _classify_year_leaves(emp_year_leaves, year, emp_is_intern, intern_until)

        # Interns' paid leave is monthly — report the balance for THIS month.
        if emp_is_intern and "paid" in balances:
            paid_used_month = sum(
                weight
                for cls in classification.values()
                if cls["type"] == "paid"
                for d, weight in cls["paid_dates"].items()
                if month_start <= d <= month_end
            )
            balances["paid"] = {
                "quota": INTERN_MONTHLY_PAID_QUOTA,
                "used": paid_used_month,
                "remaining": max(INTERN_MONTHLY_PAID_QUOTA - paid_used_month, 0.0),
                "period": "month",
            }

        # Build rows only for leaves that touch THIS month, scoping the auto
        # paid/unpaid day counts to the month being run.
        month_leaves = []
        for leave in sorted(emp_year_leaves, key=lambda lv: (lv.start_date or date_type.min, lv.id)):
            if leave.start_date is None or leave.end_date is None:
                continue
            if leave.start_date > month_end or leave.end_date < month_start:
                continue
            cls = classification.get(leave.id, {"paid_dates": {}, "unpaid_dates": {}})
            paid_in_month = sum(weight for d, weight in cls["paid_dates"].items() if month_start <= d <= month_end)
            unpaid_in_month = sum(weight for d, weight in cls["unpaid_dates"].items() if month_start <= d <= month_end)
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
            base_salary=emp_base_salary,
            bonus_limit=emp_bonus_limit, bonus=emp_bonus,
            additional_payment=emp_additional,
        )
        row["salary_missing"] = emp_base_salary is None
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


class BonusIn(BaseModel):
    employee_id: int
    amount: float


class AdditionalPaymentIn(BaseModel):
    employee_id: int
    amount: float


class SavePayrollBody(BaseModel):
    month: str
    status: str = "draft"          # "draft" or "finalized"
    notes: Optional[str] = None
    adjustments: List[LeaveAdjustmentIn]
    bonuses: List[BonusIn] = []
    additional_payments: List[AdditionalPaymentIn] = []
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
        # Delete existing adjustments + bonuses and re-insert
        db.query(PayrollLeaveAdjustment).filter(
            PayrollLeaveAdjustment.payroll_run_id == run.id
        ).delete()
        db.query(PayrollBonus).filter(
            PayrollBonus.payroll_run_id == run.id
        ).delete()
        db.query(PayrollAdditionalPayment).filter(
            PayrollAdditionalPayment.payroll_run_id == run.id
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

    # Persist bonuses, clamped server-side to each employee's limit (the salary
    # table's opt_bonus_monthly), matched by name. Amounts of 0 are skipped.
    emp_name_by_id = {e.id: e.name for e in db.query(Employee).all()}
    bonus_limit_by_name = {
        _normalize_name(s.full_name): (_read_pay(s.opt_bonus_monthly) or 0.0)
        for s in db.query(Salary).all()
    }
    for b in body.bonuses:
        limit = bonus_limit_by_name.get(_normalize_name(emp_name_by_id.get(b.employee_id, "")), 0.0)
        amount = max(0.0, min(b.amount or 0.0, limit))
        if amount <= 0:
            continue
        db.add(PayrollBonus(
            payroll_run_id=run.id,
            employee_id=b.employee_id,
            amount=round(amount, 2),
        ))

    # Persist additional payments — free-form, just clamped to be non-negative.
    for ap in body.additional_payments:
        amount = max(0.0, ap.amount or 0.0)
        if amount <= 0:
            continue
        db.add(PayrollAdditionalPayment(
            payroll_run_id=run.id,
            employee_id=ap.employee_id,
            amount=round(amount, 2),
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
        "Leave Days", "Paid Days", "Unpaid (Deducted) Days", "Deduction (₹)", "Bonus (₹)", "Additional Payments (₹)", "Final Salary (₹)", "Notes"
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
            row.get("bonus", 0),
            row.get("additional_payment", 0),
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


# ── Pay: ground-truth salary records ────────────────────────────────────────────
# The "Pay" tab manages the SOURCE salary data (each employee's monthly base
# salary). Monthly Pay derives every calculation from this same value
# (Employee.base_salary_enc), so there is a single source of truth — editing a
# salary here is exactly what the Monthly Pay preview reads. Salaries are stored
# encrypted at rest; see services/salary_crypto.py.

def _salary_record(emp: Employee) -> dict:
    base = decrypt_salary(emp.base_salary_enc)
    return {
        "employee_id": emp.id,
        "employee_name": emp.name,
        "designation": emp.designation,
        "employee_type": emp.employee_type,
        "status": emp.status,
        "currency": "INR",
        "base_salary": base,
        "salary_missing": base is None,
    }


@router.get("/salaries")
def list_salaries(db: Session = Depends(get_db)):
    """List active employees with their ground-truth monthly base salary.

    `encryption_enabled` tells the UI whether the server can persist salaries
    (SALARY_KEY configured). When False, writes are silently dropped by design,
    so the UI can warn instead of pretending a save succeeded.
    """
    employees = (
        db.query(Employee)
        .filter(Employee.status == "active")
        .order_by(Employee.name)
        .all()
    )
    return {
        "encryption_enabled": encryption_enabled(),
        "employees": [_salary_record(emp) for emp in employees],
    }


class SalaryUpdateIn(BaseModel):
    base_salary: float


@router.put("/salaries/{employee_id}")
def update_salary(employee_id: int, body: SalaryUpdateIn, db: Session = Depends(get_db)):
    """Set an employee's monthly base salary (the ground-truth Pay record).

    Mirrors the employee API's salary handling: the value is encrypted into
    base_salary_enc and the legacy plaintext column is left NULL.
    """
    if body.base_salary <= 0:
        raise HTTPException(status_code=422, detail="base_salary must be a positive amount")

    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if not encryption_enabled():
        # No SALARY_KEY → encrypt_salary would drop the value silently. Fail loudly.
        raise HTTPException(
            status_code=503,
            detail="Salary encryption is not configured on the server (SALARY_KEY unset); cannot store salary.",
        )

    emp.base_salary_enc = encrypt_salary(body.base_salary)
    emp.base_salary = None
    db.commit()
    db.refresh(emp)
    return _salary_record(emp)


# ── Salary table (read-only) ────────────────────────────────────────────────────
# The Pay tab surfaces the externally-managed `salary` table, which holds each
# person's actual pay (stored as text, e.g. "₹100,000"). The tab can edit a row's
# monthly pay and toggle its Active/Inactive status; Inactive rows are excluded
# from the Monthly Pay run. The masked mirror (`masked_salaries`) is left untouched.

def _salary_table_record(row: Salary) -> dict:
    # Pay columns are encrypted at rest — decrypt and re-format for display so the
    # UI shows real "₹100,000" values while the DB only ever holds ciphertext.
    return {
        "id": row.id,
        "full_name": row.full_name,
        "status": row.status,
        "employment_type": row.employment_type,
        "base_pay_annual": _format_money(_read_pay(row.base_pay_annual)),
        "optional_bonus_annual": _format_money(_read_pay(row.optional_bonus_annual)),
        "base_pay_monthly": _format_money(_read_pay(row.base_pay_monthly)),
        "opt_bonus_monthly": _format_money(_read_pay(row.opt_bonus_monthly)),
    }


@router.get("/salary-records")
def list_salary_records(db: Session = Depends(get_db)):
    """List every row from the salary table with actual pay values."""
    rows = db.query(Salary).order_by(Salary.id).all()
    return {"salaries": [_salary_table_record(r) for r in rows]}


class SalaryRecordUpdateIn(BaseModel):
    base_pay_monthly: float
    opt_bonus_monthly: Optional[float] = None


@router.put("/salary-records/{record_id}")
def update_salary_record(record_id: int, body: SalaryRecordUpdateIn, db: Session = Depends(get_db)):
    """Edit a salary row's monthly base pay (and optional bonus).

    Amounts come in as plain numbers and are stored ENCRYPTED at rest (Fernet),
    so the DB never holds plaintext pay. A non-positive base pay is rejected;
    bonus of 0/None clears it. Requires SALARY_KEY to be configured.
    """
    if body.base_pay_monthly <= 0:
        raise HTTPException(status_code=422, detail="base_pay_monthly must be a positive amount")

    if not encryption_enabled():
        # No SALARY_KEY → encrypt_salary would silently drop the value. Fail loudly.
        raise HTTPException(
            status_code=503,
            detail="Salary encryption is not configured on the server (SALARY_KEY unset); cannot store salary.",
        )

    row = db.query(Salary).filter(Salary.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Salary record not found")

    row.base_pay_monthly = encrypt_salary(body.base_pay_monthly)
    row.opt_bonus_monthly = encrypt_salary(body.opt_bonus_monthly) if body.opt_bonus_monthly else None
    db.commit()
    db.refresh(row)
    return _salary_table_record(row)


class SalaryStatusIn(BaseModel):
    status: str   # "Active" or "Inactive"


@router.patch("/salary-records/{record_id}/status")
def set_salary_record_status(record_id: int, body: SalaryStatusIn, db: Session = Depends(get_db)):
    """Toggle a salary row Active/Inactive. Inactive rows drop out of Monthly Pay."""
    normalized = (body.status or "").strip().lower()
    if normalized not in ("active", "inactive"):
        raise HTTPException(status_code=422, detail="status must be 'Active' or 'Inactive'")

    row = db.query(Salary).filter(Salary.id == record_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Salary record not found")

    row.status = "Active" if normalized == "active" else "Inactive"
    db.commit()
    db.refresh(row)
    return _salary_table_record(row)
