"""Side Projects API - CRUD for employee personal side projects."""
import logging
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from app.services.auth_service import get_current_user
from app.services import audit_service, project_scope
from app.models.user import User
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.allocation import Allocation
from app.models.employee import Employee
from app.models.parent_project import MainProject
from app.models.project import DailySheet
from app.models.side_project import SideProject
from app.models.sub_project import SubProject
from app.services.slack_service import (
    notify_employee_side_project_created,
    notify_pm_side_project_created,
    notify_pm_side_project_deleted,
    try_get_or_cache_employee_slack_user_id,
)

router = APIRouter(
    prefix="/api/side-projects",
    tags=["Side Projects"],
    dependencies=[Depends(get_current_user)],
)
logger = logging.getLogger(__name__)

# Unrestricted (any employee's side projects).
FULL_ACCESS_ROLES = ("admin", "hr")
# Scoped to employees on projects they manage/lead (same idea as project list/scope).
MANAGER_ROLES = ("pm", "team_lead")


def _role(user: User) -> str:
    return (user.role or "").lower()


def _has_full_access(user: User) -> bool:
    return _role(user) in FULL_ACCESS_ROLES or project_scope.has_full_access(user)


def _managed_employee_ids(db: Session, current_user: User) -> set[int]:
    """
    Employee IDs allocated on projects this user manages or leads.
    Mirrors project_scope used by the Projects API.
    """
    if not current_user.employee_id:
        return set()

    managed_project_ids = project_scope.managed_projects_of_employee(
        db, current_user, current_user.employee_id
    )
    if not managed_project_ids:
        return set()

    rows = (
        db.query(Allocation.employee_id)
        .filter(
            Allocation.sub_project_id.in_(managed_project_ids),
            Allocation.is_active == True,
        )
        .distinct()
        .all()
    )
    return {row[0] for row in rows if row[0]}


def _require_side_project_write_access(
    db: Session,
    current_user: User,
    *,
    side_project: SideProject | None = None,
    employee_id: int | None = None,
) -> None:
    """
    Write access (create / update / delete):

    - admin / hr: any side project
    - pm / team_lead: only for employees on projects they manage/lead
    - employee / other: never
    """
    target_employee_id = (
        side_project.employee_id if side_project is not None else employee_id
    )
    if target_employee_id is None:
        raise HTTPException(status_code=400, detail="employee_id is required")

    if _has_full_access(current_user):
        return

    role = _role(current_user)
    if role in MANAGER_ROLES:
        if target_employee_id in _managed_employee_ids(db, current_user):
            return
        raise HTTPException(
            status_code=403,
            detail="Not allowed to modify this side project",
        )

    # Employees and any other role: no writes
    raise HTTPException(
        status_code=403,
        detail="Not allowed to modify side projects",
    )


def _format_pm_project_line(
    project: DailySheet, allocation: Allocation, sub_project: SubProject | None
) -> str:
    sub_project_name = sub_project.name if sub_project else "Unmapped sub-project"
    hours = allocation.total_daily_hours or 0
    roles = ", ".join(allocation.role_tags or []) or "No role tags"
    return f"{project.name} ({sub_project_name}) - {hours}h/day - Roles: {roles}"


def _get_side_project_pm_targets(db: Session, employee: Employee) -> list[dict]:
    allocations = db.query(Allocation).filter(Allocation.employee_id == employee.id).all()
    if not allocations:
        return []

    project_ids = list(
        {allocation.sub_project_id for allocation in allocations if allocation.sub_project_id}
    )
    if not project_ids:
        return []

    projects = db.query(DailySheet).filter(DailySheet.id.in_(project_ids)).all()
    project_map = {project.id: project for project in projects}

    sub_project_ids = list(
        {project.sub_project_id for project in projects if project.sub_project_id}
    )
    sub_projects = (
        db.query(SubProject).filter(SubProject.id.in_(sub_project_ids)).all()
        if sub_project_ids
        else []
    )
    sub_project_map = {sub_project.id: sub_project for sub_project in sub_projects}

    main_project_ids = list(
        {project.main_project_id for project in projects if project.main_project_id}
    )
    main_projects = (
        db.query(MainProject).filter(MainProject.id.in_(main_project_ids)).all()
        if main_project_ids
        else []
    )
    main_project_map = {main_project.id: main_project for main_project in main_projects}

    pm_project_map: dict[int, list[str]] = {}
    for allocation in allocations:
        project = project_map.get(allocation.sub_project_id)
        if not project:
            continue

        sub_project = (
            sub_project_map.get(project.sub_project_id) if project.sub_project_id else None
        )
        main_project = (
            main_project_map.get(project.main_project_id) if project.main_project_id else None
        )
        pm_ids = {
            pm_id
            for pm_id in (
                getattr(sub_project, "pm_id", None),
                getattr(main_project, "program_manager_id", None),
            )
            if pm_id
        }
        if not pm_ids:
            continue

        project_line = _format_pm_project_line(project, allocation, sub_project)
        for pm_id in pm_ids:
            pm_project_map.setdefault(pm_id, [])
            if project_line not in pm_project_map[pm_id]:
                pm_project_map[pm_id].append(project_line)

    if not pm_project_map:
        return []

    pm_employees = db.query(Employee).filter(Employee.id.in_(pm_project_map.keys())).all()
    targets = []
    for pm_employee in pm_employees:
        slack_user_id = try_get_or_cache_employee_slack_user_id(db, pm_employee)
        if not slack_user_id:
            continue
        targets.append(
            {
                "pm_employee": pm_employee,
                "pm_slack_user_id": slack_user_id,
                "impacted_projects": pm_project_map.get(pm_employee.id, []),
            }
        )

    return targets


class SideProjectCreate(BaseModel):
    employee_id: int
    name: str
    description: Optional[str] = None
    status: Optional[str] = "active"
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class SideProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class SideProjectResponse(BaseModel):
    id: int
    employee_id: int
    name: str
    description: Optional[str] = None
    status: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


@router.get("", response_model=List[SideProjectResponse])
def list_side_projects(
    employee_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List scope:
    - admin / hr: all (optional employee_id filter)
    - pm / team_lead: side projects of employees on projects they manage/lead
    - employee / other: own only
    """
    role = _role(current_user)
    query = db.query(SideProject)

    if _has_full_access(current_user):
        if employee_id is not None:
            query = query.filter(SideProject.employee_id == employee_id)
    elif role in MANAGER_ROLES:
        allowed = _managed_employee_ids(db, current_user)
        if employee_id is not None:
            if employee_id not in allowed:
                return []
            query = query.filter(SideProject.employee_id == employee_id)
        else:
            if not allowed:
                return []
            query = query.filter(SideProject.employee_id.in_(allowed))
    else:
        # Employee (and any other role): own only
        if not current_user.employee_id:
            return []
        query = query.filter(SideProject.employee_id == current_user.employee_id)

    return query.order_by(SideProject.created_at.desc()).all()


@router.post("", response_model=SideProjectResponse, status_code=201)
def create_side_project(
    payload: SideProjectCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    employee = db.query(Employee).filter(Employee.id == payload.employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Employees: no create. PM/TL: only for managed employees. Admin/HR: any.
    _require_side_project_write_access(db, current_user, employee_id=payload.employee_id)

    sp = SideProject(**payload.dict())
    db.add(sp)
    db.flush()

    audit_service.record(
        db,
        actor=current_user,
        action="side_project.created",
        category="Side Projects",
        action_type="Created",
        entity_type="side_project",
        entity_id=sp.id,
        entity_name=sp.name,
        subject_employee_id=sp.employee_id,
        subject_name=employee.name,
        details=audit_service.changes(
            audit_service.field_diff("Status", None, sp.status),
            audit_service.field_diff(
                "Period",
                None,
                f"{sp.start_date} → {sp.end_date}" if sp.start_date else None,
            ),
        ),
        summary=f"Added side project '{sp.name}' for {employee.name}",
        request=http_request,
    )

    db.commit()
    db.refresh(sp)

    try:
        employee.slack_user_id = try_get_or_cache_employee_slack_user_id(db, employee)
        notify_employee_side_project_created(employee, sp)

        for target in _get_side_project_pm_targets(db, employee):
            notify_pm_side_project_created(
                pm_slack_user_id=target["pm_slack_user_id"],
                pm_name=target["pm_employee"].name,
                employee_name=employee.name,
                employee_email=employee.email,
                employee_designation=employee.designation,
                side_project_name=sp.name,
                side_project_description=sp.description,
                side_project_status=sp.status,
                start_date=sp.start_date.isoformat() if sp.start_date else None,
                end_date=sp.end_date.isoformat() if sp.end_date else None,
                impacted_projects=target["impacted_projects"],
            )
    except Exception as exc:
        logger.warning("Slack notification failed for side project %s: %s", sp.id, exc)

    return sp


@router.put("/{sp_id}", response_model=SideProjectResponse)
def update_side_project(
    sp_id: int,
    payload: SideProjectUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sp = db.query(SideProject).filter(SideProject.id == sp_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Side project not found")

    _require_side_project_write_access(db, current_user, side_project=sp)

    for key, value in payload.dict(exclude_unset=True).items():
        setattr(sp, key, value)

    db.commit()
    db.refresh(sp)
    return sp


@router.delete("/{sp_id}")
def delete_side_project(
    sp_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sp = db.query(SideProject).filter(SideProject.id == sp_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Side project not found")

    _require_side_project_write_access(db, current_user, side_project=sp)

    employee = db.query(Employee).filter(Employee.id == sp.employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    pm_targets = _get_side_project_pm_targets(db, employee)

    audit_service.record(
        db,
        actor=current_user,
        action="side_project.deleted",
        category="Side Projects",
        action_type="Deleted",
        entity_type="side_project",
        entity_id=sp.id,
        entity_name=sp.name,
        subject_employee_id=sp.employee_id,
        subject_name=employee.name,
        details=audit_service.changes(
            audit_service.field_diff("Status at deletion", sp.status, None),
        ),
        summary=f"Deleted side project '{sp.name}' for {employee.name}",
        request=http_request,
    )

    db.delete(sp)
    db.commit()

    try:
        for target in pm_targets:
            notify_pm_side_project_deleted(
                pm_slack_user_id=target["pm_slack_user_id"],
                pm_name=target["pm_employee"].name,
                employee_name=employee.name,
                employee_email=employee.email,
                employee_designation=employee.designation,
                side_project_name=sp.name,
                side_project_description=sp.description,
                side_project_status=sp.status,
                start_date=sp.start_date.isoformat() if sp.start_date else None,
                end_date=sp.end_date.isoformat() if sp.end_date else None,
                impacted_projects=target["impacted_projects"],
            )
    except Exception as exc:
        logger.warning(
            "Slack delete notification failed for side project %s: %s", sp_id, exc
        )

    return {"message": "Side project deleted"}