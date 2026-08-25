"""
Guidelines API - CRUD for project guidelines and uploaded documents.
"""
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from app.services.auth_service import get_current_user, require_role
from app.services.storage_service import delete_guideline_file, upload_guideline_file
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.guideline import Guideline
from app.models.user import User
from app.services import audit_service

router = APIRouter(prefix="/api/guidelines", tags=["Guidelines"], dependencies=[Depends(get_current_user)])

import os
_base = Path("/tmp/uploads") if os.environ.get("VERCEL") else Path(__file__).resolve().parents[2] / "uploads"
UPLOAD_DIR = _base / "guidelines"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


class GuidelineCreate(BaseModel):
    main_project_id: Optional[int] = None
    sub_project_id: Optional[int] = None
    title: str
    content: Optional[str] = None
    file_name: Optional[str] = None
    uploaded_by: Optional[int] = None


class GuidelineUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None


class GuidelineResponse(BaseModel):
    id: int
    main_project_id: Optional[int] = None
    sub_project_id: Optional[int] = None
    title: str
    content: Optional[str] = None
    file_name: Optional[str] = None
    file_url: Optional[str] = None
    uploaded_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


@router.get("", response_model=List[GuidelineResponse])
def list_guidelines(
    main_project_id: Optional[int] = None,
    sub_project_id: Optional[int] = None,
    uploaded_by: Optional[int] = None,
    db: Session = Depends(get_db),
):
    query = db.query(Guideline)
    if main_project_id:
        query = query.filter(Guideline.main_project_id == main_project_id)
    if sub_project_id:
        query = query.filter(Guideline.sub_project_id == sub_project_id)
    if uploaded_by:
        query = query.filter(Guideline.uploaded_by == uploaded_by)
    return query.order_by(Guideline.created_at.desc()).all()


@router.get("/{guideline_id}", response_model=GuidelineResponse)
def get_guideline(guideline_id: int, db: Session = Depends(get_db)):
    guideline = db.query(Guideline).filter(Guideline.id == guideline_id).first()
    if not guideline:
        raise HTTPException(status_code=404, detail="Guideline not found")
    return guideline

@router.get("/for-me", response_model=List[GuidelineResponse])
def list_guidelines_for_me(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Guidelines for projects the logged-in employee is allocated to.
    Avoids the client downloading every guideline + every project just to filter.
    """
    from app.models.allocation import Allocation
    from app.models.project import DailySheet

    if not current_user.employee_id:
        return []

    alloc_rows = (
        db.query(Allocation.sub_project_id)
        .filter(
            Allocation.employee_id == current_user.employee_id,
            Allocation.is_active == True,
            Allocation.sub_project_id.isnot(None),
        )
        .distinct()
        .all()
    )
    sub_project_ids = {r[0] for r in alloc_rows if r[0]}
    if not sub_project_ids:
        return []

    # Main-project ids for those daily sheets (org-level guidelines)
    main_rows = (
        db.query(DailySheet.main_project_id)
        .filter(DailySheet.id.in_(sub_project_ids))
        .all()
    )
    main_project_ids = {r[0] for r in main_rows if r[0]}

    from sqlalchemy import or_

    query = db.query(Guideline).filter(
        or_(
            Guideline.sub_project_id.in_(sub_project_ids),
            Guideline.main_project_id.in_(main_project_ids) if main_project_ids else False,
        )
    )
    return query.order_by(Guideline.created_at.desc()).all()

@router.post("", response_model=GuidelineResponse)
def create_guideline(
    payload: GuidelineCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    # uploaded_by is overwritten with the session user: it is the record of who put
    # this document in front of the team, so a client-supplied value can't be trusted.
    data = payload.model_dump()
    data["uploaded_by"] = current_user.id
    guideline = Guideline(**data)
    db.add(guideline)
    db.flush()

    audit_service.record(
        db,
        actor=current_user,
        action="guideline.created",
        category="Guidelines",
        action_type="Created",
        entity_type="guideline",
        entity_id=guideline.id,
        entity_name=guideline.title,
        summary=f"Created guideline '{guideline.title}'",
        request=http_request,
    )

    db.commit()
    db.refresh(guideline)
    return guideline


@router.post("/upload", response_model=GuidelineResponse)
async def upload_guideline(
    request: Request,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    main_project_id: Optional[int] = Form(None),
    sub_project_id: Optional[int] = Form(None),
    uploaded_by: Optional[int] = Form(None, deprecated=True),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    original_name = Path(file.filename or "").name
    if not original_name:
        raise HTTPException(status_code=400, detail="File name is required")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    stored_name = f"{uuid4().hex}{Path(original_name).suffix}"
    try:
        file_url = upload_guideline_file(
            file_bytes=file_bytes,
            stored_name=stored_name,
            content_type=file.content_type or "application/octet-stream",
        )
    except RuntimeError as err:
        raise HTTPException(status_code=500, detail=str(err)) from err

    guideline = Guideline(
        title=title or Path(original_name).stem,
        main_project_id=main_project_id,
        sub_project_id=sub_project_id,
        file_name=original_name,
        file_url=file_url,
        # From the session, not the form field: uploaded_by is the record of who put
        # this document in front of the team.
        uploaded_by=current_user.id,
    )
    try:
        db.add(guideline)
        db.flush()
        audit_service.record(
            db,
            actor=current_user,
            action="guideline.uploaded",
            category="Guidelines",
            action_type="Created",
            entity_type="guideline",
            entity_id=guideline.id,
            entity_name=guideline.title,
            details=audit_service.changes(
                audit_service.field_diff("File", None, original_name),
                audit_service.field_diff("Size", None, f"{len(file_bytes) // 1024} KB"),
            ),
            summary=f"Uploaded guideline document '{guideline.title}' ({original_name})",
            request=request,
        )
        db.commit()
        db.refresh(guideline)
        return guideline
    except SQLAlchemyError as exc:
        db.rollback()
        # The object is already in the bucket, so remove it — otherwise a failed insert
        # leaves an orphan no row will ever reference or clean up.
        delete_guideline_file(file_url=file_url, upload_dir=UPLOAD_DIR)
        raise HTTPException(status_code=500, detail=f"Failed to save guideline upload: {exc.__class__.__name__}") from exc


@router.put("/{guideline_id}", response_model=GuidelineResponse)
def update_guideline(
    guideline_id: int,
    payload: GuidelineUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    guideline = db.query(Guideline).filter(Guideline.id == guideline_id).first()
    if not guideline:
        raise HTTPException(status_code=404, detail="Guideline not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(guideline, key, value)

    db.commit()
    db.refresh(guideline)
    return guideline


@router.delete("/{guideline_id}")
def delete_guideline(
    guideline_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    guideline = db.query(Guideline).filter(Guideline.id == guideline_id).first()
    if not guideline:
        raise HTTPException(status_code=404, detail="Guideline not found")

    audit_service.record(
        db,
        actor=current_user,
        action="guideline.deleted",
        category="Guidelines",
        action_type="Deleted",
        entity_type="guideline",
        entity_id=guideline.id,
        entity_name=guideline.title,
        details=audit_service.changes(
            audit_service.field_diff("File", guideline.file_name, None),
        ),
        summary=(
            f"Deleted guideline '{guideline.title}'"
            + (" and its uploaded file" if guideline.file_url else "")
        ),
        request=http_request,
    )

    if guideline.file_url:
        delete_guideline_file(file_url=guideline.file_url, upload_dir=UPLOAD_DIR)

    db.delete(guideline)
    db.commit()
    return {"message": "Guideline deleted successfully"}

