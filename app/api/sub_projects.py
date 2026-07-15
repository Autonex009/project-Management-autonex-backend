"""
Sub-Projects API — The NEW intermediate hierarchy level.
Hierarchy: MainProject → SubProject → DailySheet → Allocations
"""
from fastapi import APIRouter, Depends, HTTPException
from app.services.auth_service import get_current_user, require_role
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime

from app.db.database import get_db
from app.models.sub_project import SubProject
from app.models.parent_project import MainProject

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

@router.post("", response_model=SubProjectResponse, dependencies=[Depends(require_role("admin", "pm"))])
def create_sub_project(payload: SubProjectCreate, db: Session = Depends(get_db)):
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


@router.put("/{sub_project_id}", response_model=SubProjectResponse, dependencies=[Depends(require_role("admin", "pm"))])
def update_sub_project(
    sub_project_id: int,
    payload: SubProjectUpdate,
    db: Session = Depends(get_db),
):
    sp = db.query(SubProject).filter(SubProject.id == sub_project_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Sub-project not found")

    for key, value in payload.dict(exclude_unset=True).items():
        setattr(sp, key, value)

    db.commit()
    db.refresh(sp)
    return sp


@router.delete("/{sub_project_id}", dependencies=[Depends(require_role("admin", "pm"))])
def delete_sub_project(sub_project_id: int, db: Session = Depends(get_db)):
    sp = db.query(SubProject).filter(SubProject.id == sub_project_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Sub-project not found")

    db.delete(sp)
    db.commit()
    return {"message": "Sub-project deleted successfully"}
