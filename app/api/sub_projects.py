"""
Sub-Projects API — The NEW intermediate hierarchy level.
Hierarchy: MainProject → SubProject → DailySheet → Allocations
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from app.services.auth_service import get_current_user, require_role
from app.services import audit_service
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime

from app.db.database import get_db
from app.models.sub_project import SubProject
from app.models.parent_project import MainProject
from app.models.user import User

SUB_PROJECT_FIELD_LABELS = {
    "name": "Name",
    "client": "Client",
    "pm_id": "Project manager",
    "description": "Description",
    "start_date": "Start date",
    "duration_days": "Duration (days)",
    "status": "Status",
}

router = APIRouter(prefix="/api/sub-projects-new", tags=["sub-projects-new"], dependencies=[Depends(get_current_user)])


# ── Schemas ─────────────────────────────────────────────────────────
class SubProjectCreate(BaseModel):
    main_project_id: int
    name: str
    client: Optional[str] = None
    pm_id: Optional[int] = None
    description: Optional[str] = None
    start_date: Optional[date] = None
    duration_days: Optional[int] = None
    status: Optional[str] = "active"


class SubProjectUpdate(BaseModel):
    name: Optional[str] = None
    client: Optional[str] = None
    pm_id: Optional[int] = None
    description: Optional[str] = None
    start_date: Optional[date] = None
    duration_days: Optional[int] = None
    status: Optional[str] = None


class SubProjectResponse(BaseModel):
    id: int
    main_project_id: int
    name: str
    client: Optional[str] = None
    pm_id: Optional[int] = None
    description: Optional[str] = None
    start_date: Optional[date] = None
    duration_days: Optional[int] = None
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ── Endpoints ───────────────────────────────────────────────────────

@router.post("", response_model=SubProjectResponse)
def create_sub_project(
    payload: SubProjectCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    """Create a new sub-project under a main project."""
    # Verify main project exists
    main = db.query(MainProject).filter(MainProject.id == payload.main_project_id).first()
    if not main:
        raise HTTPException(status_code=404, detail="Main project not found")

    data = payload.dict()
    # Auto-fill client from parent if not provided
    if not data.get("client"):
        data["client"] = main.client

    sp = SubProject(**data)
    db.add(sp)
    db.flush()

    audit_service.record(
        db,
        actor=current_user,
        action="sub_project.created",
        category="Projects",
        action_type="Created",
        entity_type="sub_project",
        entity_id=sp.id,
        entity_name=sp.name,
        details=audit_service.changes(
            audit_service.field_diff("Parent project", None, main.name),
            audit_service.field_diff("Client", None, sp.client),
            audit_service.field_diff("Status", None, sp.status),
        ),
        summary=f"Created sub-project {sp.name} under {main.name}",
        request=http_request,
    )

    db.commit()
    db.refresh(sp)
    return sp


@router.get("", response_model=list[SubProjectResponse])
def list_sub_projects(
    main_project_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """List all sub-projects, optionally filtered by main project."""
    query = db.query(SubProject)
    if main_project_id:
        query = query.filter(SubProject.main_project_id == main_project_id)
    return query.order_by(SubProject.id.asc()).all()


@router.get("/{sub_project_id}", response_model=SubProjectResponse)
def get_sub_project(sub_project_id: int, db: Session = Depends(get_db)):
    sp = db.query(SubProject).filter(SubProject.id == sub_project_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Sub-project not found")
    return sp


@router.put("/{sub_project_id}", response_model=SubProjectResponse)
def update_sub_project(
    sub_project_id: int,
    payload: SubProjectUpdate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    sp = db.query(SubProject).filter(SubProject.id == sub_project_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Sub-project not found")

    update_data = payload.dict(exclude_unset=True)
    before = audit_service.snapshot(sp, update_data.keys())

    for key, value in update_data.items():
        setattr(sp, key, value)

    details = audit_service.diff_all(before, sp, SUB_PROJECT_FIELD_LABELS)
    if details:
        audit_service.record(
            db,
            actor=current_user,
            action="sub_project.updated",
            category="Projects",
            action_type="Updated",
            entity_type="sub_project",
            entity_id=sp.id,
            entity_name=sp.name,
            details=details,
            summary=(
                f"Updated sub-project {sp.name} — "
                + ", ".join(d["field"] for d in details[:4])
            ),
            request=http_request,
        )

    db.commit()
    db.refresh(sp)
    return sp


@router.delete("/{sub_project_id}")
def delete_sub_project(
    sub_project_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    sp = db.query(SubProject).filter(SubProject.id == sub_project_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Sub-project not found")

    audit_service.record(
        db,
        actor=current_user,
        action="sub_project.deleted",
        category="Projects",
        action_type="Deleted",
        entity_type="sub_project",
        entity_id=sp.id,
        entity_name=sp.name,
        details=audit_service.changes(
            audit_service.field_diff("Client", sp.client, None),
            audit_service.field_diff("Status at deletion", sp.status, None),
        ),
        summary=f"Deleted sub-project {sp.name}",
        request=http_request,
    )

    db.delete(sp)
    db.commit()
    return {"message": "Sub-project deleted successfully"}
