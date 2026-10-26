"""
Company Settings API - CRUD for admin-editable key-value configuration.

Used to store WiFi credentials, office details, and other dynamic
company information that admins can update from the frontend.
"""
import json
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.company_settings import CompanySetting
from app.models.user import User
from app.models.employee import Employee
from app.services.auth_service import get_current_user, require_role
from app.services import audit_service
from app.services.project_cache import InMemoryTTLCache, get_redis_client

logger = logging.getLogger(__name__)

# Settings whose values are secrets. For these, only the fact of a change is logged.
# Substring match so variants like "office_wifi_password" are covered too.
SECRET_KEY_HINTS = ("password", "secret", "token", "key", "credential")

CANTEEN_MENU_KEY = "canteen_lunch_menu"
REDIS_MENU_CACHE_KEY = "canteen:lunch_menu:v1"
REDIS_MENU_TTL_SECONDS = 86400  # 24 Hours

# Tier 1: In-process RAM TTL Cache (0.001 ms, protects Redis during 10 AM rush)
canteen_menu_l1_cache = InMemoryTTLCache(ttl_seconds=60.0, max_size=10)


def _is_secret(key: str) -> bool:
    lowered = (key or "").lower()
    return any(hint in lowered for hint in SECRET_KEY_HINTS)


router = APIRouter(prefix="/api/company-settings", tags=["Company Settings"])


# ── Schemas ──────────────────────────────────────────────────────────

class CanteenMenuItem(BaseModel):
    id: str
    name: str = Field(..., min_length=1, max_length=100)
    is_veg: bool = True
    extra_charge: float = Field(default=0.0, ge=0.0)


class CanteenMenuPayload(BaseModel):
    items: List[CanteenMenuItem]


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

# -----------------------------------------------------------------------------
# Canteen Menu Endpoints (Dual-Tier L1 RAM + L2 Redis Global Cache)
# Declared BEFORE /{key} to avoid FastAPI path parameter collisions!
# -----------------------------------------------------------------------------
@router.get(
    "/canteen-menu",
    response_model=List[CanteenMenuItem],
    dependencies=[Depends(get_current_user)],
)
def get_canteen_menu():
    """Fetch current daily canteen lunch menu using Dual-Tier L1 (RAM) + L2 (Redis Global) cache.
    Zero DB connection checkout on cache hits, protecting the small database connection pool.
    """
    # 1. Tier 1: Check In-Process RAM Cache (Sub-microsecond, 0 DB / 0 Redis sockets)
    cached_l1 = canteen_menu_l1_cache.get("menu")
    if cached_l1 is not None:
        return cached_l1

    # 2. Tier 2: Check Redis Global Cache (Shared across all workers & dynos, ~0.8 ms)
    redis_client = get_redis_client()
    if redis_client:
        try:
            cached_redis = redis_client.get(REDIS_MENU_CACHE_KEY)
            if cached_redis:
                items = json.loads(cached_redis)
                canteen_menu_l1_cache.set("menu", items)
                return items
        except Exception as exc:
            logger.debug("company_settings: Redis menu read failed (%s), falling back to DB", exc)

    # 3. Tier 3: Cold Fallback to PostgreSQL (company_settings table)
    from app.db.database import SessionLocal
    db = SessionLocal()
    try:
        setting = db.query(CompanySetting).filter(CompanySetting.key == CANTEEN_MENU_KEY).first()
        if not setting or not setting.value:
            items = [
                {"id": "default-1", "name": "Veg Thali", "is_veg": True, "extra_charge": 0.0},
            ]
        else:
            try:
                items = json.loads(setting.value)
                if not isinstance(items, list):
                    items = []
            except Exception:
                items = []
    finally:
        db.close()

    # Backfill L1 RAM and L2 Redis
    canteen_menu_l1_cache.set("menu", items)
    if redis_client:
        try:
            redis_client.set(REDIS_MENU_CACHE_KEY, json.dumps(items), ex=REDIS_MENU_TTL_SECONDS)
        except Exception as exc:
            logger.debug("company_settings: Redis menu backfill failed: %s", exc)

    return items


@router.put(
    "/canteen-menu",
    response_model=List[CanteenMenuItem],
    dependencies=[Depends(require_role("admin", "hr"))],
)
def update_canteen_menu(
    payload: CanteenMenuPayload,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "hr")),
):
    """Update daily canteen lunch menu (Admin and HR only).
    Immediately syncs PostgreSQL, flushes L2 Redis globally, and clears L1 RAM.
    """
    cleaned_items = [item.model_dump() for item in payload.items]
    json_val = json.dumps(cleaned_items)

    setting = db.query(CompanySetting).filter(CompanySetting.key == CANTEEN_MENU_KEY).first()
    existed = setting is not None

    if setting:
        setting.value = json_val
        setting.updated_by = current_user.id
    else:
        setting = CompanySetting(
            key=CANTEEN_MENU_KEY,
            value=json_val,
            updated_by=current_user.id,
        )
        db.add(setting)

    db.flush()

    # Record Audit Trail
    audit_service.record(
        db,
        actor=current_user,
        action="company_setting.canteen_menu_updated",
        category="Settings",
        action_type="Updated" if existed else "Created",
        entity_type="company_setting",
        entity_id=setting.id,
        entity_name="canteen_lunch_menu",
        details=audit_service.changes(
            audit_service.field_diff("Menu Items Count", None, len(cleaned_items))
        ),
        summary=f"Updated daily canteen lunch menu ({len(cleaned_items)} items configured)",
        request=http_request,
    )

    db.commit()
    db.refresh(setting)

    # Invalidate and Sync Caching Layers
    canteen_menu_l1_cache.set("menu", cleaned_items)
    redis_client = get_redis_client()
    if redis_client:
        try:
            redis_client.set(REDIS_MENU_CACHE_KEY, json_val, ex=REDIS_MENU_TTL_SECONDS)
        except Exception as exc:
            logger.debug("company_settings: Redis menu sync failed: %s", exc)

    return cleaned_items


# -----------------------------------------------------------------------------
# General Company Settings CRUD (Admin Only)
# -----------------------------------------------------------------------------
@router.get("", response_model=List[CompanySettingResponse], dependencies=[Depends(require_role("admin"))])
def list_settings(db: Session = Depends(get_db)):
    """Return all company settings."""
    return db.query(CompanySetting).order_by(CompanySetting.key).all()


@router.get("/{key}", response_model=CompanySettingResponse, dependencies=[Depends(require_role("admin"))])
def get_setting(key: str, db: Session = Depends(get_db)):
    """Return a single setting by its key."""
    setting = db.query(CompanySetting).filter(CompanySetting.key == key).first()
    if not setting:
        raise HTTPException(status_code=404, detail=f"Setting '{key}' not found")
    return setting


@router.put("/{key}", response_model=CompanySettingResponse, dependencies=[Depends(require_role("admin"))])
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


@router.delete("/{key}", dependencies=[Depends(require_role("admin"))])
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
