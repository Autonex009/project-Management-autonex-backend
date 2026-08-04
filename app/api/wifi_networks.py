"""
WiFi Networks API - CRUD for office WiFi credentials.
Admins can add/edit/delete multiple WiFi networks.
All authenticated users can read them.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from app.services.auth_service import require_role
from app.services import audit_service
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import User
from app.models.wifi_network import WifiNetwork

router = APIRouter(prefix="/api/wifi-networks", tags=["WiFi Networks"], dependencies=[Depends(require_role("admin"))])


# ── Schemas ──────────────────────────────────────────────────────────

class WifiNetworkCreate(BaseModel):
    name: str
    password: Optional[str] = None
    updated_by: Optional[int] = None


class WifiNetworkUpdate(BaseModel):
    name: Optional[str] = None
    password: Optional[str] = None
    updated_by: Optional[int] = None


class WifiNetworkResponse(BaseModel):
    id: int
    name: str
    password: Optional[str] = None
    updated_by: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── Endpoints ────────────────────────────────────────────────────────

@router.get("", response_model=List[WifiNetworkResponse])
def list_wifi_networks(db: Session = Depends(get_db)):
    """Return all WiFi networks."""
    return db.query(WifiNetwork).order_by(WifiNetwork.id).all()


@router.get("/{wifi_id}", response_model=WifiNetworkResponse)
def get_wifi_network(wifi_id: int, db: Session = Depends(get_db)):
    """Return a single WiFi network by ID."""
    wifi = db.query(WifiNetwork).filter(WifiNetwork.id == wifi_id).first()
    if not wifi:
        raise HTTPException(status_code=404, detail="WiFi network not found")
    return wifi


@router.post("", response_model=WifiNetworkResponse)
def create_wifi_network(
    payload: WifiNetworkCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Add a new WiFi network (admin-only)."""
    # updated_by records who last touched the shared credential — session, not body.
    data = payload.model_dump()
    data["updated_by"] = current_user.id
    wifi = WifiNetwork(**data)
    db.add(wifi)
    db.flush()

    # The WiFi password is a shared secret, so only its presence is recorded — the
    # audit log must not become a second place it is readable.
    audit_service.record(
        db,
        actor=current_user,
        action="wifi_network.created",
        category="Settings",
        action_type="Created",
        entity_type="wifi_network",
        entity_id=wifi.id,
        entity_name=wifi.name,
        details=audit_service.changes(
            audit_service.field_diff("Password", None, "set" if wifi.password else None),
        ),
        summary=f"Added WiFi network {wifi.name}",
        request=http_request,
    )

    db.commit()
    db.refresh(wifi)
    return wifi


@router.put("/{wifi_id}", response_model=WifiNetworkResponse)
def update_wifi_network(
    wifi_id: int,
    payload: WifiNetworkUpdate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Update an existing WiFi network (admin-only)."""
    wifi = db.query(WifiNetwork).filter(WifiNetwork.id == wifi_id).first()
    if not wifi:
        raise HTTPException(status_code=404, detail="WiFi network not found")

    update_data = payload.model_dump(exclude_unset=True)
    old_name = wifi.name
    password_changed = "password" in update_data and update_data["password"] != wifi.password

    # Same reason as on create: never let the caller decide who the change is attributed to.
    update_data.pop("updated_by", None)
    for key, value in update_data.items():
        setattr(wifi, key, value)
    wifi.updated_by = current_user.id

    audit_service.record(
        db,
        actor=current_user,
        action="wifi_network.updated",
        category="Settings",
        action_type="Updated",
        entity_type="wifi_network",
        entity_id=wifi.id,
        entity_name=wifi.name,
        details=audit_service.changes(
            audit_service.field_diff("Name", old_name, wifi.name),
            # Again: that it changed, never to what.
            audit_service.field_diff("Password", None, "changed" if password_changed else None),
        ),
        summary=(
            f"Updated WiFi network {wifi.name}"
            + (" — password rotated" if password_changed else "")
        ),
        request=http_request,
    )

    db.commit()
    db.refresh(wifi)
    return wifi


@router.delete("/{wifi_id}")
def delete_wifi_network(
    wifi_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Delete a WiFi network (admin-only)."""
    wifi = db.query(WifiNetwork).filter(WifiNetwork.id == wifi_id).first()
    if not wifi:
        raise HTTPException(status_code=404, detail="WiFi network not found")

    audit_service.record(
        db,
        actor=current_user,
        action="wifi_network.deleted",
        category="Settings",
        action_type="Deleted",
        entity_type="wifi_network",
        entity_id=wifi.id,
        entity_name=wifi.name,
        summary=f"Deleted WiFi network {wifi.name}",
        request=http_request,
    )

    db.delete(wifi)
    db.commit()
    return {"message": "WiFi network deleted successfully"}
