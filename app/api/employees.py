from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4
import logging
import os

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from app.db.database import get_db
from app.constants.leave_types import is_intern, is_intern_or_contractor
from app.services.salary_crypto import encrypt_salary
from app.models.allocation import Allocation
from app.models.employee import Employee
from app.models.leave import Leave
from app.models.notification import Notification
from app.models.side_project import SideProject
from app.models.user import User
from app.models.wfh import WFHRequest
from app.models.payroll import Salary
from app.schemas.employee import (
    EmployeeCreate,
    EmployeeUpdate,
    EmployeeResponse,
)
from app.services.auth_service import (
    get_current_user,
    has_team_read,
    hash_password,
    require_role,
)
from app.services.email_service import try_send_email_changed_email
from app.services.identity_validator import check_duplicate_identity
from app.services import audit_service

# Column → display name for audit diffs. Anything unmapped falls back to a humanised
# column name, so a new field still shows up rather than being silently dropped.
# Keys must be real Employee columns — see app/models/employee.py.
EMPLOYEE_FIELD_LABELS = {
    "name": "Name",
    "email": "Email",
    "phone": "Phone",
    "designation": "Designation",
    "employee_type": "Employee type",
    "status": "Status",
    "working_hours_per_day": "Working hours/day",
    "weekly_availability": "Weekly availability",
    "productivity_baseline": "Productivity baseline",
    "skills": "Skills",
    "encord_id": "Encord ID",
    "razorpay_email": "Razorpay email",
    "slack_user_id": "Slack user ID",
    "mentor_id": "Mentor",
}


from app.services.slack_service import (
    try_get_or_cache_employee_slack_user_id,
    try_lookup_user_avatar_by_email,
)

# Avatars live in Supabase Storage, never on disk. The host filesystem is
# ephemeral on both Railway and Vercel, so a locally-written file is gone by the
# next deploy while employees.avatar_url still points at it — the picture then
# 404s and the UI silently falls back to initials, which reads as data loss.
from app.services.storage_service import (
    AVATAR_BUCKET,
    delete_from_bucket,
    is_supabase_configured,
    upload_to_bucket,
)

# Extension -> the Content-Type Supabase should serve the object with. Doubles as
# the allowlist, so the two can never disagree.
AVATAR_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
ALLOWED_AVATAR_EXTENSIONS = set(AVATAR_CONTENT_TYPES)
MAX_AVATAR_BYTES = 5 * 1024 * 1024  # 5 MB

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/employees",
    tags=["Employees"],
    dependencies=[Depends(get_current_user)],
)

DEFAULT_EMPLOYEE_PASSWORD = "emp123"

# Self-service email changes are confined to the company domain — an employee can
# move their login onto their real work address, but not onto an arbitrary one.
COMPANY_EMAIL_DOMAIN = os.getenv("COMPANY_EMAIL_DOMAIN", "autonexai360.com").strip().lower().lstrip("@")
EMPLOYEE_PORTAL_URL = os.getenv("EMPLOYEE_PORTAL_URL", "https://pmportal.autonexai360.com/login/employee")
DESIGNATION_ROLE_MAP = {
    "Admin": "admin",
    "HR": "hr",   # combined Admin + PM access
    "Program Manager": "pm",
    # Reads everything a PM reads, approves nothing — see services/project_scope.py.
    # Must stay in step with DESIGNATION_ACCESS in api/auth.py: that map decides the
    # token role at login, this one decides the stored users.role. Because the role is
    # re-derived from the designation on every employee update (below), omitting this
    # entry would demote a team lead to "employee" on an unrelated profile edit.
    "Team Lead": "team_lead",
    "Annotator/ Reviewer": "employee",
    "Annotator/Reviewer": "employee",
    "Annotator": "employee",
    "Reviewer": "employee",
    "Developer": "employee",
}


def get_user_role_from_designation(designation: str | None) -> str:
    return DESIGNATION_ROLE_MAP.get(designation, "employee")


def check_employee_access(employee: Employee, current_user: User):
    if has_team_read(current_user):
        return
    is_self = (current_user.employee_id == employee.id) or (current_user.email == employee.email)
    if not is_self:
        raise HTTPException(status_code=403, detail="Access denied")


# ✅ CREATE EMPLOYEE
@router.post("", response_model=EmployeeResponse)
def create_employee(
    payload: EmployeeCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
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

    audit_service.record(
        db,
        actor=current_user,
        action="employee.created",
        category="Employees",
        action_type="Created",
        entity_type="employee",
        entity_id=employee.id,
        entity_name=employee.name,
        subject_employee_id=employee.id,
        subject_name=employee.name,
        details=audit_service.changes(
            audit_service.field_diff("Designation", None, employee.designation),
            audit_service.field_diff("Employee type", None, employee.employee_type),
            audit_service.field_diff("Email", None, employee.email),
            audit_service.field_diff("Login role", None, user.role),
            # Deliberately records only *whether* a salary was set, never the figure —
            # the plaintext lives nowhere and the audit log must not become the one place
            # it does.
            audit_service.field_diff(
                "Salary", None, "set" if plain_salary is not None else None
            ),
        ),
        summary=(
            f"Created employee {employee.name} ({employee.designation or 'no designation'})"
            f" with a {user.role} login"
        ),
        request=http_request,
    )

    db.commit()
    db.refresh(employee)
    return employee


# ✅ LIST EMPLOYEES
@router.get("", response_model=list[EmployeeResponse], dependencies=[Depends(require_role("admin", "pm"))])
def list_employees(
    status: str = None,
    include_archived: bool = False,
    search: Optional[str] = Query(
        default=None,
        max_length=200,
        description="Substring match on name, email or Encord ID (case-insensitive).",
    ),
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
    if search and search.strip():
        # Encord IDs (annotator247_theta@encord.ai) are the reason this filter exists
        # server-side: they are only worth typing in full, and a chunk of the roster
        # has none, so the client cannot reliably answer "who owns this ID?".
        # ``trim`` on the column because some stored IDs carry leading whitespace
        # from the sheet they were imported from — without it those never match.
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Employee.name.ilike(term),
                Employee.email.ilike(term),
                func.trim(Employee.encord_id).ilike(term),
            )
        )
    return query.all()


@router.get("/status/active", response_model=list[EmployeeResponse], dependencies=[Depends(require_role("admin", "pm"))])
def get_active_employees(db: Session = Depends(get_db)):
    return db.query(Employee).filter(Employee.status == "active").all()


@router.get("/status/inactive", response_model=list[EmployeeResponse], dependencies=[Depends(require_role("admin", "pm"))])
def get_inactive_employees(db: Session = Depends(get_db)):
    return db.query(Employee).filter(Employee.status == "inactive").all()


@router.get("/status/idle", response_model=list[EmployeeResponse], dependencies=[Depends(require_role("admin", "pm"))])
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    check_employee_access(employee, current_user)
    return employee


# ✅ UPDATE EMPLOYEE
@router.put("/{employee_id}", response_model=EmployeeResponse)
def update_employee(
    employee_id: int,
    payload: EmployeeUpdate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    check_employee_access(employee, current_user)
    
    # Deliberately NOT has_team_read: this restricts *writing*, and a team lead reads
    # everything without being allowed to change designations, salary or status. Adding
    # "team_lead" here would let a view-only role rewrite these fields.
    if current_user.role not in ["admin", "pm", "hr"]:
        # `email` is here so the company-domain rule on PATCH /{id}/email can't be
        # sidestepped by patching the field directly — self-service email changes
        # must go through that endpoint, which validates the domain and notifies.
        admin_only_fields = {
            "email",
            "employee_type", "designation", "working_hours_per_day",
            "weekly_availability", "productivity_baseline", "status", "base_salary"
        }
        requested_fields = set(payload.dict(exclude_unset=True).keys())
        blocked = requested_fields.intersection(admin_only_fields)
        if blocked:
            raise HTTPException(status_code=403, detail=f"Cannot update administrative fields: {', '.join(blocked)}")
    
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
    salary_changed = "base_salary" in update_data
    if salary_changed:
        employee.base_salary_enc = encrypt_salary(update_data.pop("base_salary"))
        employee.base_salary = None

    # Snapshot before the writes land, so each diff has a real "from" side.
    before = audit_service.snapshot(employee, update_data.keys())
    old_role = None
    linked_user_for_role = db.query(User).filter(User.employee_id == employee.id).first()
    if linked_user_for_role:
        old_role = linked_user_for_role.role

    for key, value in update_data.items():
        setattr(employee, key, value)

    linked_user = db.query(User).filter(User.employee_id == employee.id).first()
    if linked_user:
        linked_user.email = employee.email
        linked_user.name = employee.name
        linked_user.role = get_user_role_from_designation(employee.designation)
        linked_user.skills = employee.skills or []

    details = audit_service.diff_all(before, employee, EMPLOYEE_FIELD_LABELS)
    # A designation change silently rewrites the login role, which is a permissions
    # change — surface it explicitly rather than leaving it implied by "Designation".
    if linked_user and old_role != linked_user.role:
        details += audit_service.changes(
            audit_service.field_diff("Login role", old_role, linked_user.role)
        )
    if salary_changed:
        details += audit_service.changes(
            audit_service.field_diff("Salary", "(encrypted)", "changed")
        )

    if details:
        audit_service.record(
            db,
            actor=current_user,
            action="employee.updated",
            category="Employees",
            action_type="Updated",
            entity_type="employee",
            entity_id=employee.id,
            entity_name=employee.name,
            subject_employee_id=employee.id,
            subject_name=employee.name,
            details=details,
            summary=(
                f"Updated {employee.name} — "
                + ", ".join(d["field"] for d in details[:4])
                + (f" and {len(details) - 4} more" if len(details) > 4 else "")
            ),
            request=http_request,
        )

    db.commit()
    db.refresh(employee)
    return employee


class ChangeEmailBody(BaseModel):
    new_email: EmailStr


@router.patch("/{employee_id}/email", response_model=EmployeeResponse)
def change_employee_email(
    employee_id: int,
    body: ChangeEmailBody,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Move an employee's login onto their real company address.

    Restricted to @{COMPANY_EMAIL_DOMAIN} so an employee can correct a personal
    address to their work one, but can't point their login at somewhere arbitrary.

    Updates Employee.email AND the linked User.email (login reads User.email), and
    leaves password_hash untouched — the same password keeps working, which is what
    the confirmation email tells them.
    """
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Self, or an admin/PM acting on their behalf.
    check_employee_access(employee, current_user)

    new_email = (body.new_email or "").strip().lower()
    old_email = employee.email or ""

    domain = new_email.rsplit("@", 1)[-1]
    if domain != COMPANY_EMAIL_DOMAIN:
        raise HTTPException(
            status_code=422,
            detail=f"Your login email must be a @{COMPANY_EMAIL_DOMAIN} address.",
        )

    if new_email == old_email.strip().lower():
        raise HTTPException(
            status_code=400,
            detail="That is already your current email address.",
        )

    linked_user = db.query(User).filter(User.employee_id == employee.id).first()
    # Excluding this person's own records so their existing rows don't self-conflict.
    check_duplicate_identity(
        db,
        email=new_email,
        exclude_employee_id=employee.id,
        exclude_user_id=linked_user.id if linked_user else None,
    )

    employee.email = new_email
    if linked_user:
        linked_user.email = new_email

    # This moves which address can log in as this person, so it is a credential
    # change, not a profile edit. Both addresses are recorded.
    audit_service.record(
        db,
        actor=current_user,
        action="employee.email_changed",
        category="Employees",
        action_type="Updated",
        entity_type="employee",
        entity_id=employee.id,
        entity_name=employee.name,
        subject_employee_id=employee.id,
        subject_name=employee.name,
        details=audit_service.changes(
            audit_service.field_diff("Login email", old_email, new_email),
        ),
        summary=(
            f"Changed login email for {employee.name}: {old_email} → {new_email}"
            + ("" if current_user.employee_id == employee.id else " (changed by an admin)")
        ),
        request=http_request,
    )
    db.commit()
    db.refresh(employee)

    # Best-effort: the change is already committed, so a mail failure must not undo
    # it or 500 the request. It's logged, and the new address is on screen anyway.
    sent = try_send_email_changed_email(
        to_email=new_email,
        to_name=employee.name or new_email,
        old_email=old_email,
        portal_url=EMPLOYEE_PORTAL_URL,
    )
    logger.info(
        "[change-email] employee id=%s %s -> %s (login row %s, notice %s)",
        employee.id, old_email, new_email,
        "updated" if linked_user else "MISSING",
        "sent" if sent else "FAILED",
    )

    return employee


class ConvertToFulltimeBody(BaseModel):
    converted_by: Optional[int] = None   # user_id of the admin performing the promotion
    designation: Optional[str] = None    # optionally update designation on promotion


# ✅ CONVERT INTERN → FULL-TIME (in place — preserves all linked history)
@router.post("/{employee_id}/convert-to-fulltime", response_model=EmployeeResponse)
def convert_to_fulltime(
    employee_id: int,
    http_request: Request,
    body: ConvertToFulltimeBody = ConvertToFulltimeBody(),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
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

    if not is_intern_or_contractor(employee.employee_type):
        raise HTTPException(
            status_code=400,
            detail=f"Only interns or contractors can be converted to full-time (current type: {employee.employee_type}).",
        )

    old_type = employee.employee_type
    old_designation = employee.designation

    employee.previous_employee_type = employee.employee_type
    employee.employee_type = "Full-time"
    employee.converted_to_fulltime_at = datetime.now(timezone.utc).replace(tzinfo=None)
    # Promoter comes from the session. body.converted_by is client-supplied and
    # therefore not trustworthy for a record of who made the decision.
    employee.converted_by = current_user.id
    if body.designation:
        employee.designation = body.designation

    # Keep the linked auth user's role and employment type in sync (designation may have changed).
    linked_user = db.query(User).filter(User.employee_id == employee.id).first()
    old_login_role = linked_user.role if linked_user else None
    if linked_user:
        linked_user.role = get_user_role_from_designation(employee.designation)
        linked_user.employment_type = "Full-time"
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

    # Keep corresponding salary record in sync
    salary_record = db.query(Salary).filter(Salary.full_name == employee.name).first()
    if salary_record:
        salary_record.employment_type = "Full-time"
    audit_service.record(
        db,
        actor=current_user,
        action="employee.promoted_fulltime",
        category="Employees",
        action_type="Promoted",
        entity_type="employee",
        entity_id=employee.id,
        entity_name=employee.name,
        subject_employee_id=employee.id,
        subject_name=employee.name,
        details=audit_service.changes(
            audit_service.field_diff("Employee type", old_type, employee.employee_type),
            audit_service.field_diff("Designation", old_designation, employee.designation),
            audit_service.field_diff(
                "Login role", old_login_role, linked_user.role if linked_user else None
            ),
        ),
        summary=(
            f"Promoted {employee.name} from {old_type} to Full-time"
            + (f" as {employee.designation}" if body.designation else "")
            + " — full-time leave entitlements now apply"
        ),
        request=http_request,
    )

    db.commit()
    db.refresh(employee)
    return employee


# ✅ DELETE EMPLOYEE (SOFT-ARCHIVE)
@router.delete("/{employee_id}")
def delete_employee(
    employee_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()

    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    try:
        old_status = employee.status
        employee.status = "archived"

        # Deactivate associated user account
        linked_user = db.query(User).filter(User.employee_id == employee.id).first()
        if not linked_user:
            linked_user = db.query(User).filter(User.email == employee.email).first()
        if linked_user:
            linked_user.is_active = False

        # Clear allocations for this employee — counted before the delete so the entry
        # can say how many project assignments this silently removed.
        removed_allocations = db.query(Allocation).filter(
            Allocation.employee_id == employee.id
        ).count()
        db.query(Allocation).filter(Allocation.employee_id == employee.id).delete(synchronize_session=False)
        db.flush()

        audit_service.record(
            db,
            actor=current_user,
            action="employee.archived",
            category="Employees",
            action_type="Archived",
            entity_type="employee",
            entity_id=employee.id,
            entity_name=employee.name,
            subject_employee_id=employee.id,
            subject_name=employee.name,
            details=audit_service.changes(
                audit_service.field_diff("Status", old_status, "archived"),
                audit_service.field_diff(
                    "Login access", "active", "disabled" if linked_user else None
                ),
                audit_service.field_diff(
                    "Project allocations removed", None, removed_allocations or None
                ),
            ),
            summary=(
                f"Archived {employee.name} — login disabled"
                + (
                    f" and {removed_allocations} project allocation"
                    f"{'s' if removed_allocations != 1 else ''} removed"
                    if removed_allocations
                    else ""
                )
            ),
            request=http_request,
        )

        db.commit()
        return {"message": "Employee archived successfully"}
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to archive employee")


# ✅ RESTORE ARCHIVED EMPLOYEE
@router.post("/{employee_id}/restore", response_model=EmployeeResponse)
def restore_employee(
    employee_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()

    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    try:
        old_status = employee.status
        employee.status = "active"

        # Reactivate associated user account
        linked_user = db.query(User).filter(User.employee_id == employee.id).first()
        if not linked_user:
            linked_user = db.query(User).filter(User.email == employee.email).first()
        if linked_user:
            linked_user.is_active = True

        audit_service.record(
            db,
            actor=current_user,
            action="employee.restored",
            category="Employees",
            action_type="Restored",
            entity_type="employee",
            entity_id=employee.id,
            entity_name=employee.name,
            subject_employee_id=employee.id,
            subject_name=employee.name,
            details=audit_service.changes(
                audit_service.field_diff("Status", old_status, "active"),
                audit_service.field_diff(
                    "Login access", "disabled", "active" if linked_user else None
                ),
            ),
            summary=(
                f"Restored {employee.name} from archived — login re-enabled. "
                f"Previous project allocations are not restored."
            ),
            request=http_request,
        )

        db.commit()
        db.refresh(employee)
        return employee
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to restore employee")


# ✅ EMPLOYEE AVAILABILITY (±30 days)
@router.get("/{employee_id}/availability", dependencies=[Depends(require_role("admin", "pm"))])
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
@router.get("/{employee_id}/slack-link", dependencies=[Depends(require_role("admin", "pm"))])
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


# ── Profile picture (avatar) ────────────────────────────────────────
@router.post("/{employee_id}/avatar", response_model=EmployeeResponse)
async def upload_employee_avatar(
    employee_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Upload a profile picture for an employee and store its public URL."""
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    check_employee_access(employee, current_user)

    original_name = Path(file.filename or "").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_AVATAR_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported image type. Use JPG, PNG, GIF or WEBP.",
        )

    if not is_supabase_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Image storage is not configured. Set SUPABASE_URL and "
                "SUPABASE_SERVICE_ROLE_KEY on the server."
            ),
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(file_bytes) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=400, detail="Image is too large (max 5 MB)")

    stored_name = f"{uuid4().hex}{suffix}"
    try:
        public_url = upload_to_bucket(
            AVATAR_BUCKET, file_bytes, stored_name, AVATAR_CONTENT_TYPES[suffix]
        )
    except RuntimeError as exc:
        logger.warning("Avatar upload failed for employee %s: %s", employee_id, exc)
        raise HTTPException(
            status_code=502, detail="Could not store the image. Please try again."
        ) from exc

    # Cleared only once the new URL is safely committed — losing the old picture
    # on a failed save would leave the employee with no avatar at all.
    old_url = employee.avatar_url or ""
    employee.avatar_url = public_url
    try:
        # Deliberately thin — the image URL itself is noise, so only the fact of the
        # upload is recorded, plus a note when an admin changed someone else's picture.
        audit_service.record(
            db,
            actor=current_user,
            action="employee.avatar_uploaded",
            category="Employees",
            action_type="Updated",
            entity_type="employee",
            entity_id=employee.id,
            entity_name=employee.name,
            subject_employee_id=employee.id,
            subject_name=employee.name,
            # "via computer" distinguishes this from the Slack import below. The
            # two are indistinguishable afterwards — both leave a URL on the row —
            # so where a picture came from only survives if the log says so.
            summary=(
                f"Uploaded a new profile picture via computer for {employee.name}"
                + (
                    ""
                    if current_user.employee_id == employee.id
                    else " (done by an admin)"
                )
            ),
            request=request,
        )
        db.commit()
        db.refresh(employee)
    except SQLAlchemyError as exc:
        db.rollback()
        # The row never took the new URL, so the object we just pushed is an
        # orphan. Drop it rather than leave it billing storage forever.
        delete_from_bucket(AVATAR_BUCKET, public_url)
        raise HTTPException(status_code=500, detail="Failed to save avatar") from exc

    # Best-effort, and after the commit: a failure here costs a stray object, not
    # the employee's picture. Legacy /uploads/avatars/ URLs simply do not match.
    delete_from_bucket(AVATAR_BUCKET, old_url)
    return employee


@router.post("/{employee_id}/avatar/from-slack", response_model=EmployeeResponse)
def set_employee_avatar_from_slack(
    employee_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Fetch the employee's Slack profile photo and store it as their avatar."""
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    check_employee_access(employee, current_user)

    avatar_url = try_lookup_user_avatar_by_email(employee.email)
    if not avatar_url:
        raise HTTPException(
            status_code=404,
            detail="No Slack profile photo found for this employee",
        )

    employee.avatar_url = avatar_url
    # Recorded like the upload above, and worded to say where the picture came
    # from. This path used to write silently, so the log showed a picture arriving
    # by upload and nothing at all when one was pulled from Slack.
    audit_service.record(
        db,
        actor=current_user,
        action="employee.avatar_from_slack",
        category="Employees",
        action_type="Updated",
        entity_type="employee",
        entity_id=employee.id,
        entity_name=employee.name,
        subject_employee_id=employee.id,
        subject_name=employee.name,
        summary=(
            f"Uploaded a new profile picture via Slack for {employee.name}"
            + (
                ""
                if current_user.employee_id == employee.id
                else " (done by an admin)"
            )
        ),
        request=request,
    )
    db.commit()
    db.refresh(employee)
    return employee


@router.delete("/{employee_id}/avatar", response_model=EmployeeResponse)
def delete_employee_avatar(
    employee_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Remove an employee's profile picture."""
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    check_employee_access(employee, current_user)

    old_url = employee.avatar_url or ""
    employee.avatar_url = None
    db.commit()
    db.refresh(employee)
    # After the commit: the row is what the UI reads, so clearing it is the part
    # that must succeed. A Slack URL or a legacy /uploads/ one is not ours to
    # delete and is skipped.
    delete_from_bucket(AVATAR_BUCKET, old_url)
    return employee
