"""
Company Settings API - CRUD for admin-editable key-value configuration.

Used to store WiFi credentials, office details, and other dynamic
company information that admins can update from the frontend.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from app.services.auth_service import require_role
from app.services import audit_service
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.company_settings import CompanySetting
from app.models.user import User

# Settings whose values are secrets. For these, only the fact of a change is logged.
# Substring match so variants like "office_wifi_password" are covered too.
SECRET_KEY_HINTS = ("password", "secret", "token", "key", "credential")


def _is_secret(key: str) -> bool:
    lowered = (key or "").lower()
    return any(hint in lowered for hint in SECRET_KEY_HINTS)

router = APIRouter(prefix="/api/company-settings", tags=["Company Settings"], dependencies=[Depends(require_role("admin"))])


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
def upsert_setting(
    key: str,
    payload: CompanySettingUpsert,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Create or update a setting by its key (admin-only)."""
    setting = db.query(CompanySetting).filter(CompanySetting.key == key).first()
    existed = setting is not None
    old_value = setting.value if setting else None

    if setting:
        setting.value = payload.value
        # updated_by comes from the session, not the payload — the client's value is
        # unverified and this column is the record of who last touched the setting.
        setting.updated_by = current_user.id
    else:
        setting = CompanySetting(
            key=key,
            value=payload.value,
            updated_by=current_user.id,
        )
        db.add(setting)
    db.flush()

    secret = _is_secret(key)
    audit_service.record(
        db,
        actor=current_user,
        action="company_setting.updated" if existed else "company_setting.created",
        category="Settings",
        action_type="Updated" if existed else "Created",
        entity_type="company_setting",
        entity_id=setting.id,
        entity_name=key,
        details=audit_service.changes(
            audit_service.field_diff(
                key,
                "(hidden)" if secret and old_value else old_value,
                "(hidden)" if secret else setting.value,
            ),
        ),
        summary=(
            f"{'Updated' if existed else 'Created'} company setting '{key}'"
            + (" (value hidden — secret)" if secret else "")
        ),
        request=http_request,
    )

    db.commit()
    db.refresh(setting)
    return setting


@router.delete("/{key}")
def delete_setting(
    key: str,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Delete a setting by its key (admin-only)."""
    setting = db.query(CompanySetting).filter(CompanySetting.key == key).first()
    if not setting:
        raise HTTPException(status_code=404, detail=f"Setting '{key}' not found")

    audit_service.record(
        db,
        actor=current_user,
        action="company_setting.deleted",
        category="Settings",
        action_type="Deleted",
        entity_type="company_setting",
        entity_id=setting.id,
        entity_name=key,
        details=audit_service.changes(
            audit_service.field_diff(
                key, "(hidden)" if _is_secret(key) else setting.value, None
            ),
        ),
        summary=f"Deleted company setting '{key}'",
        request=http_request,
    )

    db.delete(setting)
    db.commit()
    return {"message": f"Setting '{key}' deleted successfully"}
