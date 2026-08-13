from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.constants.badges_catalog import BADGE_CATALOG, VALID_BADGE_CODES

from app.models.employee import Employee
from app.models.employee_badge import EmployeeBadge, EmployeeBadgeLog
from app.models.user import User
from app.schemas.badge import EmployeeBadgeCreate
from app.services.auth_service import get_current_user, has_team_read, require_role
from app.services.badge_service import award_badge, expire_due_badges, revoke_badge
from app.services import audit_service

router = APIRouter(
    prefix="/api",
    tags=["Badges"],
    dependencies=[Depends(get_current_user)],
)


def _ensure_employee_access(employee_id: int, current_user: User) -> None:
    if has_team_read(current_user):
        return
    if current_user.employee_id != employee_id:
        raise HTTPException(status_code=403, detail="Access denied")


def _badge_name(code: str) -> Optional[str]:
    item = BADGE_CATALOG.get(code)
    return item["name"] if item else None


def _enrich_badge(db: Session, badge: EmployeeBadge) -> dict:
    employee = db.query(Employee).filter(Employee.id == badge.employee_id).first()
    return {
        "id": badge.id,
        "employee_id": badge.employee_id,
        "employee_name": employee.name if employee else None,
        "badge_code": badge.badge_code,
        "badge_name": _badge_name(badge.badge_code),
        "period_start": badge.period_start,
        "period_end": badge.period_end,
        "expires_at": badge.expires_at,
        "status": badge.status,
        "awarded_at": badge.awarded_at,
        "awarded_by": badge.awarded_by,
        "meta": badge.meta,
        "created_at": badge.created_at,
        "updated_at": badge.updated_at,
    }


def _enrich_log(db: Session, log: EmployeeBadgeLog) -> dict:
    employee = db.query(Employee).filter(Employee.id == log.employee_id).first()
    return {
        "id": log.id,
        "employee_badge_id": log.employee_badge_id,
        "employee_id": log.employee_id,
        "employee_name": employee.name if employee else None,
        "badge_code": log.badge_code,
        "badge_name": _badge_name(log.badge_code),
        "action": log.action,
        "period_start": log.period_start,
        "period_end": log.period_end,
        "details": log.details,
        "actor_id": log.actor_id,
        "created_at": log.created_at,
    }


@router.get("/badges/catalog", response_model=List[dict])
def get_badge_catalog(current_user: User = Depends(get_current_user)):
    return list(BADGE_CATALOG.values())


@router.get("/employee-badges/by-employee/{employee_id}", response_model=List[dict])
def list_badges_by_employee(
    employee_id: int,
    status: Optional[str] = Query(None, description="active | expired | revoked"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    _ensure_employee_access(employee_id, current_user)

    query = db.query(EmployeeBadge).filter(EmployeeBadge.employee_id == employee_id)
    if status:
        query = query.filter(EmployeeBadge.status == status)

    badges = query.order_by(EmployeeBadge.awarded_at.desc(), EmployeeBadge.id.desc()).all()
    return [_enrich_badge(db, b) for b in badges]


@router.get("/employee-badge-logs", response_model=List[dict])
def list_badge_logs(
    employee_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(EmployeeBadgeLog)

    if has_team_read(current_user):
        if employee_id is not None:
            query = query.filter(EmployeeBadgeLog.employee_id == employee_id)
    else:
        if not current_user.employee_id:
            raise HTTPException(status_code=403, detail="Access denied")
        query = query.filter(EmployeeBadgeLog.employee_id == current_user.employee_id)

    logs = query.order_by(EmployeeBadgeLog.created_at.desc(), EmployeeBadgeLog.id.desc()).all()
    return [_enrich_log(db, row) for row in logs]


@router.post("/employee-badges", response_model=dict)
def create_employee_badge(
    payload: EmployeeBadgeCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    if payload.badge_code not in VALID_BADGE_CODES:
        raise HTTPException(status_code=400, detail="Invalid badge_code")

    employee = db.query(Employee).filter(Employee.id == payload.employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    badge = award_badge(
        db,
        employee_id=payload.employee_id,
        badge_code=payload.badge_code,
        period_start=payload.period_start,
        period_end=payload.period_end,
        expires_at=payload.expires_at,
        meta=payload.meta,
        actor_id=current_user.id,
    )
    if badge is None:
        raise HTTPException(
            status_code=409,
            detail="Badge already awarded for this employee and period, or invalid code",
        )

    audit_service.record(
        db,
        actor=current_user,
        action="badge.awarded",
        category="Employees",
        action_type="Created",
        entity_type="employee_badge",
        entity_id=badge.id,
        entity_name=_badge_name(badge.badge_code) or badge.badge_code,
        subject_employee_id=employee.id,
        subject_name=employee.name,
        details=audit_service.changes(
            audit_service.field_diff("Badge", None, badge.badge_code),
            audit_service.field_diff(
                "Period",
                None,
                f"{badge.period_start} → {badge.period_end}" if badge.period_start else None,
            ),
        ),
        summary=f"Awarded {badge.badge_code} to {employee.name}",
        request=http_request,
    )

    db.commit()
    db.refresh(badge)
    return _enrich_badge(db, badge)


@router.post("/employee-badges/{badge_id}/revoke", response_model=dict)
def revoke_employee_badge(
    badge_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    badge = db.query(EmployeeBadge).filter(EmployeeBadge.id == badge_id).first()
    if not badge:
        raise HTTPException(status_code=404, detail="Badge not found")

    employee = db.query(Employee).filter(Employee.id == badge.employee_id).first()
    revoke_badge(db, badge, actor_id=current_user.id)

    audit_service.record(
        db,
        actor=current_user,
        action="badge.revoked",
        category="Employees",
        action_type="Updated",
        entity_type="employee_badge",
        entity_id=badge.id,
        entity_name=_badge_name(badge.badge_code) or badge.badge_code,
        subject_employee_id=badge.employee_id,
        subject_name=employee.name if employee else None,
        details=audit_service.changes(
            audit_service.field_diff("Status", "active", "revoked"),
        ),
        summary=f"Revoked {badge.badge_code} from {employee.name if employee else 'employee'}",
        request=http_request,
    )

    db.commit()
    db.refresh(badge)
    return _enrich_badge(db, badge)


@router.post("/badges/expire")
def run_badge_expiry(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    count = expire_due_badges(db)
    db.commit()
    return {"expired_count": count}