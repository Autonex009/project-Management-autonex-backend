"""
Office IPs API - CRUD for authorized office public IP addresses.
Admins can add/edit/delete office IP addresses or CIDR blocks.
These IPs are used to verify employee presence on Office Wi-Fi during check-in.
"""
import ipaddress
import logging
import os
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.office_ip import OfficeIP
from app.models.user import User
from app.services import audit_service
from app.services.auth_service import require_role

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/office-ips",
    tags=["Office IPs"],
    dependencies=[Depends(require_role("admin"))],
)

DEFAULT_INITIAL_IPS = [
    # Floor 7
    ("38.20.140.122", "Autonex_Annotators703-2", "Floor 7"),
    ("103.54.189.22", "smart_manufacturing_team", "Floor 7"),
    ("27.0.150.66", "Autonex_Annotators703", "Floor 7"),
    # Floor 9
    ("38.20.140.122", "Autonex-jeebr", "Floor 9"),
    ("103.19.135.178", "Autonex-Blazenet", "Floor 9"),
    ("38.20.140.154", "Autonex-jeebr-2.4G", "Floor 9"),
    # Floor 17
    ("38.20.140.122", "Autonex1710", "Floor 17"),
]


def validate_ip_or_cidr(ip_str: str) -> str:
    cleaned = (ip_str or "").strip()
    if not cleaned:
        raise ValueError("IP address cannot be empty")
    try:
        if "/" in cleaned:
            network = ipaddress.ip_network(cleaned, strict=False)
            return str(network)
        else:
            addr = ipaddress.ip_address(cleaned)
            return str(addr)
    except ValueError:
        raise ValueError(f"'{ip_str}' is not a valid IPv4/IPv6 address or CIDR range")


# ── Schemas ──────────────────────────────────────────────────────────

class OfficeIPCreate(BaseModel):
    ip_address: str
    label: Optional[str] = None
    floor: Optional[str] = None

    @field_validator("ip_address")
    @classmethod
    def check_ip(cls, v: str) -> str:
        return validate_ip_or_cidr(v)


class OfficeIPUpdate(BaseModel):
    ip_address: Optional[str] = None
    label: Optional[str] = None
    floor: Optional[str] = None

    @field_validator("ip_address")
    @classmethod
    def check_ip(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return validate_ip_or_cidr(v)
        return v


class OfficeIPResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ip_address: str
    label: Optional[str] = None
    floor: Optional[str] = None
    updated_by: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# ── Seeding Helper ───────────────────────────────────────────────────

def ensure_initial_office_ips(db: Session):
    """Ensure column schema, drop single-IP unique constraint, and seed initial office IPs if empty or missing."""
    try:
        db.execute(text("ALTER TABLE office_ips ADD COLUMN IF NOT EXISTS floor TEXT;"))
        db.execute(text("ALTER TABLE office_ips DROP CONSTRAINT IF EXISTS office_ips_ip_address_key;"))
        db.commit()
    except Exception:
        db.rollback()

    existing_records = db.query(OfficeIP).all()
    if not existing_records:
        raw_env = os.getenv("OFFICE_IPS", "")
        items_to_seed = []
        if raw_env.strip():
            for ip in raw_env.split(","):
                clean = ip.strip()
                if clean:
                    items_to_seed.append((clean, "Office Network", None))
        else:
            items_to_seed = DEFAULT_INITIAL_IPS

        for ip_addr, label, flr in items_to_seed:
            try:
                valid_ip = validate_ip_or_cidr(ip_addr)
                db.add(OfficeIP(ip_address=valid_ip, label=label, floor=flr))
            except ValueError:
                continue
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning("Could not auto-seed office IPs: %s", e)


# ── Endpoints ────────────────────────────────────────────────────────

@router.get("", response_model=List[OfficeIPResponse])
def list_office_ips(db: Session = Depends(get_db)):
    """Return all configured office IP addresses."""
    ensure_initial_office_ips(db)
    return db.query(OfficeIP).order_by(OfficeIP.id).all()


@router.get("/{ip_id}", response_model=OfficeIPResponse)
def get_office_ip(ip_id: int, db: Session = Depends(get_db)):
    """Return a single office IP by ID."""
    item = db.query(OfficeIP).filter(OfficeIP.id == ip_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Office IP not found")
    return item


@router.post("", response_model=OfficeIPResponse)
def create_office_ip(
    payload: OfficeIPCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Add a new authorized office IP address (admin-only). Multiple networks on different floors can share the same IP."""
    norm_label = payload.label.strip() if payload.label and payload.label.strip() else None
    norm_floor = payload.floor.strip() if payload.floor and payload.floor.strip() else None

    # Check duplicate on same IP, floor, and label
    duplicate = (
        db.query(OfficeIP)
        .filter(
            OfficeIP.ip_address == payload.ip_address,
            OfficeIP.floor == norm_floor,
            OfficeIP.label == norm_label,
        )
        .first()
    )
    if duplicate:
        raise HTTPException(
            status_code=400,
            detail=f"Network with IP '{payload.ip_address}' for {norm_floor or 'office'} with label '{norm_label or ''}' is already added.",
        )

    item = OfficeIP(
        ip_address=payload.ip_address,
        label=norm_label,
        floor=norm_floor,
        updated_by=current_user.id,
    )
    db.add(item)
    db.flush()

    audit_service.record(
        db,
        actor=current_user,
        action="office_ip.created",
        category="Settings",
        action_type="Created",
        entity_type="office_ip",
        entity_id=item.id,
        entity_name=item.ip_address,
        details=audit_service.changes(
            audit_service.field_diff("IP Address", None, item.ip_address),
            audit_service.field_diff("Label", None, item.label),
            audit_service.field_diff("Floor", None, item.floor),
        ),
        summary=f"Added authorized office IP {item.ip_address} ({item.label or 'No label'})",
        request=http_request,
    )

    db.commit()
    db.refresh(item)
    return item


@router.put("/{ip_id}", response_model=OfficeIPResponse)
def update_office_ip(
    ip_id: int,
    payload: OfficeIPUpdate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Update an existing office IP address, label, or floor (admin-only)."""
    item = db.query(OfficeIP).filter(OfficeIP.id == ip_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Office IP not found")

    old_ip = item.ip_address
    old_label = item.label
    old_floor = item.floor

    target_ip = payload.ip_address if payload.ip_address is not None else item.ip_address
    target_label = payload.label.strip() if payload.label is not None and payload.label.strip() else (item.label if payload.label is None else None)
    target_floor = payload.floor.strip() if payload.floor is not None and payload.floor.strip() else (item.floor if payload.floor is None else None)

    duplicate = (
        db.query(OfficeIP)
        .filter(
            OfficeIP.id != ip_id,
            OfficeIP.ip_address == target_ip,
            OfficeIP.floor == target_floor,
            OfficeIP.label == target_label,
        )
        .first()
    )
    if duplicate:
        raise HTTPException(
            status_code=400,
            detail=f"An office network with IP '{target_ip}', floor '{target_floor or ''}' and label '{target_label or ''}' already exists.",
        )

    item.ip_address = target_ip
    item.label = target_label
    item.floor = target_floor
    item.updated_by = current_user.id

    audit_service.record(
        db,
        actor=current_user,
        action="office_ip.updated",
        category="Settings",
        action_type="Updated",
        entity_type="office_ip",
        entity_id=item.id,
        entity_name=item.ip_address,
        details=audit_service.changes(
            audit_service.field_diff("IP Address", old_ip, item.ip_address),
            audit_service.field_diff("Label", old_label, item.label),
            audit_service.field_diff("Floor", old_floor, item.floor),
        ),
        summary=f"Updated office IP {item.ip_address}",
        request=http_request,
    )

    db.commit()
    db.refresh(item)
    return item


@router.delete("/{ip_id}")
def delete_office_ip(
    ip_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Delete an authorized office IP address (admin-only)."""
    item = db.query(OfficeIP).filter(OfficeIP.id == ip_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Office IP not found")

    audit_service.record(
        db,
        actor=current_user,
        action="office_ip.deleted",
        category="Settings",
        action_type="Deleted",
        entity_type="office_ip",
        entity_id=item.id,
        entity_name=item.ip_address,
        summary=f"Deleted authorized office IP {item.ip_address} ({item.label or 'No label'})",
        request=http_request,
    )

    db.delete(item)
    db.commit()
    return {"message": "Office IP deleted successfully"}
