from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from app.db.database import get_db
from app.constants.leave_types import is_intern
from app.services.salary_crypto import encrypt_salary
from app.models.allocation import Allocation
from app.models.employee import Employee
from app.models.leave import Leave
from app.models.notification import Notification
from app.models.side_project import SideProject
from app.models.user import User
from app.models.wfh import WFHRequest
from app.schemas.employee import (
    EmployeeCreate,
    EmployeeUpdate,
    EmployeeResponse,
)
from app.services.auth_service import hash_password
from app.services.identity_validator import check_duplicate_identity
from app.services.slack_service import try_get_or_cache_employee_slack_user_id

router = APIRouter(
    prefix="/api/employees",
    tags=["Employees"],
)

DEFAULT_EMPLOYEE_PASSWORD = "emp123"
DESIGNATION_ROLE_MAP = {
    "Admin": "admin",
    "Program Manager": "pm",
    "Annotator/ Reviewer": "employee",
    "Annotator/Reviewer": "employee",
    "Annotator": "employee",
    "Reviewer": "employee",
    "Developer": "employee",
}


def get_user_role_from_designation(designation: str | None) -> str:
    return DESIGNATION_ROLE_MAP.get(designation, "employee")


# ✅ CREATE EMPLOYEE
@router.post("", response_model=EmployeeResponse)
def create_employee(
    payload: EmployeeCreate,
    db: Session = Depends(get_db)
):
    # Enforce unique physical identity check
    check_duplicate_identity(db, email=payload.email, phone=payload.phone)

    data = payload.dict()
    # Salary is encrypted at rest — never store the plaintext column.
    plain_salary = data.pop("base_salary", None)
    employee = Employee(**data)
    if plain_salary is not None:
        employee.base_salary_enc = encrypt_salary(plain_salary)
    db.add(employee)
    db.flush()

    user = User(
        email=employee.email,
        password_hash=hash_password(DEFAULT_EMPLOYEE_PASSWORD),
        name=employee.name,
        role=get_user_role_from_designation(employee.designation),
        employee_id=employee.id,
        skills=employee.skills or [],
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(employee)
    return employee


# ✅ LIST EMPLOYEES
@router.get("", response_model=list[EmployeeResponse])
def list_employees(
    status: str = None,
    include_archived: bool = False,
    db: Session = Depends(get_db)
):
    query = db.query(Employee)
    if status == "idle":
        allocated_employee_ids = db.query(Allocation.employee_id).distinct()
        query = query.filter(Employee.status == "active", Employee.id.notin_(allocated_employee_ids))
    elif status:
        query = query.filter(Employee.status == status)
    elif not include_archived:
        query = query.filter(Employee.status != "archived")
    return query.all()


@router.get("/status/active", response_model=list[EmployeeResponse])
def get_active_employees(db: Session = Depends(get_db)):
    return db.query(Employee).filter(Employee.status == "active").all()


@router.get("/status/inactive", response_model=list[EmployeeResponse])
def get_inactive_employees(db: Session = Depends(get_db)):
    return db.query(Employee).filter(Employee.status == "inactive").all()


@router.get("/status/idle", response_model=list[EmployeeResponse])
def get_idle_employees(db: Session = Depends(get_db)):
    allocated_employee_ids = db.query(Allocation.employee_id).distinct()
    return db.query(Employee).filter(
        Employee.status == "active",
        Employee.id.notin_(allocated_employee_ids)
    ).all()



# ✅ GET EMPLOYEE BY ID
@router.get("/{employee_id}", response_model=EmployeeResponse)
def get_employee(
    employee_id: int,
    db: Session = Depends(get_db)
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    return employee


# ✅ UPDATE EMPLOYEE
@router.put("/{employee_id}", response_model=EmployeeResponse)
def update_employee(
    employee_id: int,
    payload: EmployeeUpdate,
    db: Session = Depends(get_db),
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    # Ensure update doesn't introduce duplicate email or phone for another person
    if payload.email or payload.phone:
        check_duplicate_identity(
            db,
            email=payload.email if payload.email is not None else employee.email,
            phone=payload.phone if payload.phone is not None else employee.phone,
            exclude_employee_id=employee_id
        )
    
    update_data = payload.dict(exclude_unset=True)
    # Salary is encrypted at rest — divert it to the encrypted column and keep
    # the plaintext column NULL.
    if "base_salary" in update_data:
        employee.base_salary_enc = encrypt_salary(update_data.pop("base_salary"))
        employee.base_salary = None
    for key, value in update_data.items():
        setattr(employee, key, value)

    linked_user = db.query(User).filter(User.employee_id == employee.id).first()
    if linked_user:
        linked_user.email = employee.email
        linked_user.name = employee.name
        linked_user.role = get_user_role_from_designation(employee.designation)
        linked_user.skills = employee.skills or []
    
    db.commit()
    db.refresh(employee)
    return employee


class ConvertToFulltimeBody(BaseModel):
    converted_by: Optional[int] = None   # user_id of the admin performing the promotion
    designation: Optional[str] = None    # optionally update designation on promotion


# ✅ CONVERT INTERN → FULL-TIME (in place — preserves all linked history)
@router.post("/{employee_id}/convert-to-fulltime", response_model=EmployeeResponse)
def convert_to_fulltime(
    employee_id: int,
    body: ConvertToFulltimeBody = ConvertToFulltimeBody(),
    db: Session = Depends(get_db),
):
    """Promote an intern to a full-time employee WITHOUT creating a new record.

    The same employee row is updated in place, so every linked record (leaves,
    WFH, payroll, performance, allocations, documents, …) is preserved unchanged.
    Only employment type (and optionally designation) changes; full-time leave
    policy then applies automatically via employee_type. The promotion is audited
    via converted_to_fulltime_at / converted_by / previous_employee_type.
    """
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    if not is_intern(employee.employee_type):
        raise HTTPException(
            status_code=400,
            detail=f"Only interns can be converted to full-time (current type: {employee.employee_type}).",
        )

    employee.previous_employee_type = employee.employee_type
    employee.employee_type = "Full-time"
    employee.converted_to_fulltime_at = datetime.now(timezone.utc).replace(tzinfo=None)
    employee.converted_by = body.converted_by
    if body.designation:
        employee.designation = body.designation

    # Keep the linked auth user's role in sync (designation may have changed).
    linked_user = db.query(User).filter(User.employee_id == employee.id).first()
    if linked_user:
        linked_user.role = get_user_role_from_designation(employee.designation)
        # In-app audit/notification for the employee.
        db.add(Notification(
            user_id=linked_user.id,
            title="Converted to Full-time",
            message=(
                f"Your employment type has been updated to Full-time"
                f"{f' ({employee.designation})' if employee.designation else ''}. "
                "Full-time leave entitlements now apply."
            ),
            type="employee_converted",
        ))

    db.commit()
    db.refresh(employee)
    return employee


# ✅ DELETE EMPLOYEE (SOFT-ARCHIVE)
@router.delete("/{employee_id}")
def delete_employee(
    employee_id: int,
    db: Session = Depends(get_db),
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    try:
        employee.status = "archived"
        
        # Deactivate associated user account
        linked_user = db.query(User).filter(User.employee_id == employee.id).first()
        if not linked_user:
            linked_user = db.query(User).filter(User.email == employee.email).first()
        if linked_user:
            linked_user.is_active = False

        # Clear allocations for this employee
        db.query(Allocation).filter(Allocation.employee_id == employee.id).delete(synchronize_session=False)
        db.flush()

        db.commit()
        return {"message": "Employee archived successfully"}
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to archive employee")


# ✅ RESTORE ARCHIVED EMPLOYEE
@router.post("/{employee_id}/restore", response_model=EmployeeResponse)
def restore_employee(
    employee_id: int,
    db: Session = Depends(get_db),
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    try:
        employee.status = "active"
        
        # Reactivate associated user account
        linked_user = db.query(User).filter(User.employee_id == employee.id).first()
        if not linked_user:
            linked_user = db.query(User).filter(User.email == employee.email).first()
        if linked_user:
            linked_user.is_active = True

        db.commit()
        db.refresh(employee)
        return employee
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to restore employee")


# ✅ EMPLOYEE AVAILABILITY (±30 days)
@router.get("/{employee_id}/availability")
def get_employee_availability(employee_id: int, db: Session = Depends(get_db)):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    today = date.today()
    next_30 = today + timedelta(days=30)
    past_30 = today - timedelta(days=30)

    upcoming_leaves = (
        db.query(Leave)
        .filter(
            Leave.employee_id == employee_id,
            Leave.status != "rejected",
            Leave.end_date >= today,
            Leave.start_date <= next_30,
        )
        .order_by(Leave.start_date)
        .all()
    )

    past_leaves = (
        db.query(Leave)
        .filter(
            Leave.employee_id == employee_id,
            Leave.status != "rejected",
            Leave.end_date >= past_30,
            Leave.end_date < today,
        )
        .order_by(Leave.start_date.desc())
        .all()
    )

    upcoming_wfh = (
        db.query(WFHRequest)
        .filter(
            WFHRequest.employee_id == employee_id,
            WFHRequest.status != "rejected",
            WFHRequest.wfh_date >= today,
            WFHRequest.wfh_date <= next_30,
        )
        .order_by(WFHRequest.wfh_date)
        .all()
    )

    past_wfh = (
        db.query(WFHRequest)
        .filter(
            WFHRequest.employee_id == employee_id,
            WFHRequest.status != "rejected",
            WFHRequest.wfh_date >= past_30,
            WFHRequest.wfh_date < today,
        )
        .order_by(WFHRequest.wfh_date.desc())
        .all()
    )

    def expand_leave(leave):
        days = []
        d = leave.start_date
        while d <= leave.end_date:
            if d >= today:
                days.append(d.isoformat())
            d += timedelta(days=1)
        return {
            "leave_id": leave.id,
            "start_date": leave.start_date.isoformat(),
            "end_date": leave.end_date.isoformat(),
            "leave_type": leave.leave_type,
            "status": leave.status,
            "reason": leave.reason,
            "days": days,
        }

    def format_past_leave(leave):
        return {
            "leave_id": leave.id,
            "start_date": leave.start_date.isoformat(),
            "end_date": leave.end_date.isoformat(),
            "leave_type": leave.leave_type,
            "status": leave.status,
            "reason": leave.reason,
        }

    upcoming_leave_items = [expand_leave(l) for l in upcoming_leaves]

    return {
        "employee_id": employee.id,
        "employee_name": employee.name,
        "employee_email": employee.email,
        "designation": employee.designation,
        "status": employee.status,
        "today": today.isoformat(),
        "available_next_30_days": len(upcoming_leave_items) == 0,
        "upcoming_leaves": upcoming_leave_items,
        "upcoming_wfh": [
            {"id": w.id, "date": w.wfh_date.isoformat(), "status": w.status, "reason": w.reason}
            for w in upcoming_wfh
        ],
        "past_leaves": [format_past_leave(l) for l in past_leaves],
        "past_wfh": [
            {"id": w.id, "date": w.wfh_date.isoformat(), "status": w.status, "reason": w.reason}
            for w in past_wfh
        ],
    }


# ✅ SLACK DM DEEP-LINK (resolve/cache the employee's Slack user id on demand)
@router.get("/{employee_id}/slack-link")
def get_employee_slack_link(employee_id: int, db: Session = Depends(get_db)):
    """Return a browser deep-link that opens a Slack DM with the employee.

    The Slack user id is resolved (and cached) on demand from the employee's
    email when it isn't already stored. Returns 404 when the employee can't be
    matched to a Slack account.
    """
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    slack_user_id = try_get_or_cache_employee_slack_user_id(db, employee)
    if not slack_user_id:
        raise HTTPException(
            status_code=404,
            detail="No Slack account found for this employee",
        )

    # app_redirect opens the conversation in the browser (or the desktop app if
    # installed) without needing the workspace/team id.
    return {
        "employee_id": employee.id,
        "slack_user_id": slack_user_id,
        "url": f"https://slack.com/app_redirect?channel={slack_user_id}",
    }
