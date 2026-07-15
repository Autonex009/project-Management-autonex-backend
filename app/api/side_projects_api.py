"""Side Projects API - CRUD for employee personal side projects."""
import logging
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from app.services.auth_service import get_current_user
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

router = APIRouter(prefix="/api/side-projects", tags=["Side Projects"], dependencies=[Depends(get_current_user)])
logger = logging.getLogger(__name__)


def _format_pm_project_line(project: DailySheet, allocation: Allocation, sub_project: SubProject | None) -> str:
    sub_project_name = sub_project.name if sub_project else "Unmapped sub-project"
    hours = allocation.total_daily_hours or 0
    roles = ", ".join(allocation.role_tags or []) or "No role tags"
    return f"{project.name} ({sub_project_name}) - {hours}h/day - Roles: {roles}"


def _get_side_project_pm_targets(db: Session, employee: Employee) -> list[dict]:
    allocations = db.query(Allocation).filter(Allocation.employee_id == employee.id).all()
    if not allocations:
        return []

    project_ids = list({allocation.sub_project_id for allocation in allocations if allocation.sub_project_id})
    if not project_ids:
        return []

    projects = db.query(DailySheet).filter(DailySheet.id.in_(project_ids)).all()
    project_map = {project.id: project for project in projects}

    sub_project_ids = list({project.sub_project_id for project in projects if project.sub_project_id})
    sub_projects = db.query(SubProject).filter(SubProject.id.in_(sub_project_ids)).all() if sub_project_ids else []
    sub_project_map = {sub_project.id: sub_project for sub_project in sub_projects}

    main_project_ids = list({project.main_project_id for project in projects if project.main_project_id})
    main_projects = db.query(MainProject).filter(MainProject.id.in_(main_project_ids)).all() if main_project_ids else []
    main_project_map = {main_project.id: main_project for main_project in main_projects}

    pm_project_map: dict[int, list[str]] = {}
    for allocation in allocations:
        project = project_map.get(allocation.sub_project_id)
        if not project:
            continue

        sub_project = sub_project_map.get(project.sub_project_id) if project.sub_project_id else None
        main_project = main_project_map.get(project.main_project_id) if project.main_project_id else None
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
def list_side_projects(employee_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(SideProject)
    if employee_id:
        query = query.filter(SideProject.employee_id == employee_id)
    return query.order_by(SideProject.created_at.desc()).all()


@router.post("", response_model=SideProjectResponse, status_code=201)
def create_side_project(payload: SideProjectCreate, db: Session = Depends(get_db)):
    employee = db.query(Employee).filter(Employee.id == payload.employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    sp = SideProject(**payload.dict())
    db.add(sp)
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
def update_side_project(sp_id: int, payload: SideProjectUpdate, db: Session = Depends(get_db)):
    sp = db.query(SideProject).filter(SideProject.id == sp_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Side project not found")
    for key, value in payload.dict(exclude_unset=True).items():
        setattr(sp, key, value)
    db.commit()
    db.refresh(sp)
    return sp


@router.delete("/{sp_id}")
def delete_side_project(sp_id: int, db: Session = Depends(get_db)):
    sp = db.query(SideProject).filter(SideProject.id == sp_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Side project not found")

    employee = db.query(Employee).filter(Employee.id == sp.employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    pm_targets = _get_side_project_pm_targets(db, employee)
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
        logger.warning("Slack delete notification failed for side project %s: %s", sp_id, exc)

    return {"message": "Side project deleted"}
