"""
Company Settings API - CRUD for admin-editable key-value configuration.

Used to store WiFi credentials, office details, and other dynamic
company information that admins can update from the frontend.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from app.services.auth_service import get_current_user
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.company_settings import CompanySetting

router = APIRouter(prefix="/api/company-settings", tags=["Company Settings"], dependencies=[Depends(get_current_user)])


# ── Schemas ──────────────────────────────────────────────────────────

class CompanySettingUpsert(BaseModel):
    value: Optional[str] = None
    updated_by: Optional[int] = None


class CompanySettingResponse(BaseModel):
    id: int
    key: str
    value: Optional[str] = None
    updated_by: Optional[int] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── Endpoints ────────────────────────────────────────────────────────

@router.get("", response_model=List[CompanySettingResponse])
def list_settings(db: Session = Depends(get_db)):
    """Return all company settings."""
    return db.query(CompanySetting).order_by(CompanySetting.key).all()


@router.get("/{key}", response_model=CompanySettingResponse)
def get_setting(key: str, db: Session = Depends(get_db)):
    """Return a single setting by its key."""
    setting = db.query(CompanySetting).filter(CompanySetting.key == key).first()
    if not setting:
        raise HTTPException(status_code=404, detail=f"Setting '{key}' not found")
    return setting


@router.put("/{key}", response_model=CompanySettingResponse)
def upsert_setting(key: str, payload: CompanySettingUpsert, db: Session = Depends(get_db)):
    """Create or update a setting by its key (admin-only)."""
    setting = db.query(CompanySetting).filter(CompanySetting.key == key).first()
    if setting:
        setting.value = payload.value
        if payload.updated_by is not None:
            setting.updated_by = payload.updated_by
    else:
        setting = CompanySetting(
            key=key,
            value=payload.value,
            updated_by=payload.updated_by,
        )
        db.add(setting)
    db.commit()
    db.refresh(setting)
    return setting


@router.delete("/{key}")
def delete_setting(key: str, db: Session = Depends(get_db)):
    """Delete a setting by its key (admin-only)."""
    setting = db.query(CompanySetting).filter(CompanySetting.key == key).first()
    if not setting:
        raise HTTPException(status_code=404, detail=f"Setting '{key}' not found")
    db.delete(setting)
    db.commit()
    return {"message": f"Setting '{key}' deleted successfully"}
