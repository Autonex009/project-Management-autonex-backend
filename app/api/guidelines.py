"""
Guidelines API - CRUD for project guidelines and uploaded documents.
"""
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from app.services.auth_service import get_current_user, require_role
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.guideline import Guideline

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


@router.post("", response_model=GuidelineResponse, dependencies=[Depends(require_role("admin", "pm"))])
def create_guideline(payload: GuidelineCreate, db: Session = Depends(get_db)):
    guideline = Guideline(**payload.model_dump())
    db.add(guideline)
    db.commit()
    db.refresh(guideline)
    return guideline


@router.post("/upload", response_model=GuidelineResponse, dependencies=[Depends(require_role("admin", "pm"))])
async def upload_guideline(
    request: Request,
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    main_project_id: Optional[int] = Form(None),
    sub_project_id: Optional[int] = Form(None),
    uploaded_by: Optional[int] = Form(None),
    db: Session = Depends(get_db),
):
    original_name = Path(file.filename or "").name
    if not original_name:
        raise HTTPException(status_code=400, detail="File name is required")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    stored_name = f"{uuid4().hex}{Path(original_name).suffix}"
    destination = UPLOAD_DIR / stored_name
    destination.write_bytes(file_bytes)

    guideline = Guideline(
        title=title or Path(original_name).stem,
        main_project_id=main_project_id,
        sub_project_id=sub_project_id,
        file_name=original_name,
        file_url=str(request.base_url).rstrip("/") + f"/uploads/guidelines/{stored_name}",
        uploaded_by=uploaded_by,
    )
    try:
        db.add(guideline)
        db.commit()
        db.refresh(guideline)
        return guideline
    except SQLAlchemyError as exc:
        db.rollback()
        if destination.exists():
            destination.unlink()
        raise HTTPException(status_code=500, detail=f"Failed to save guideline upload: {exc.__class__.__name__}") from exc


@router.put("/{guideline_id}", response_model=GuidelineResponse, dependencies=[Depends(require_role("admin", "pm"))])
def update_guideline(guideline_id: int, payload: GuidelineUpdate, db: Session = Depends(get_db)):
    guideline = db.query(Guideline).filter(Guideline.id == guideline_id).first()
    if not guideline:
        raise HTTPException(status_code=404, detail="Guideline not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(guideline, key, value)

    db.commit()
    db.refresh(guideline)
    return guideline


@router.delete("/{guideline_id}", dependencies=[Depends(require_role("admin", "pm"))])
def delete_guideline(guideline_id: int, db: Session = Depends(get_db)):
    guideline = db.query(Guideline).filter(Guideline.id == guideline_id).first()
    if not guideline:
        raise HTTPException(status_code=404, detail="Guideline not found")

    if guideline.file_url and "/uploads/guidelines/" in guideline.file_url:
        stored_name = guideline.file_url.rsplit("/", 1)[-1]
        stored_file = UPLOAD_DIR / stored_name
        if stored_file.exists():
            stored_file.unlink()

    db.delete(guideline)
    db.commit()
    return {"message": "Guideline deleted successfully"}
