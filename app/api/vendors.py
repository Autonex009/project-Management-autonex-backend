from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.services.auth_service import get_current_user
from app.services import audit_service
from app.models.user import User
from app.models.vendor import Vendor

router = APIRouter(prefix="/api/vendors", tags=["Vendors"], dependencies=[Depends(get_current_user)])


class VendorCreate(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def clean_name(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("Vendor name is required")
        return v


class VendorResponse(BaseModel):
    id: int
    name: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


@router.get("", response_model=List[VendorResponse])
def list_vendors(db: Session = Depends(get_db)):
    return db.query(Vendor).order_by(Vendor.name.asc()).all()


@router.post("", response_model=VendorResponse, status_code=201)
def create_vendor(
    payload: VendorCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    existing = db.query(Vendor).filter(Vendor.name.ilike(payload.name)).first()
    if existing:
        # Idempotent reuse: nothing changed, so nothing is logged. Recording a
        # "created" entry here would invent an event that never happened.
        return existing
    vendor = Vendor(name=payload.name)
    db.add(vendor)
    db.flush()

    audit_service.record(
        db,
        actor=current_user,
        action="vendor.created",
        category="Vendors",
        action_type="Created",
        entity_type="vendor",
        entity_id=vendor.id,
        entity_name=vendor.name,
        summary=f"Added vendor {vendor.name}",
        request=http_request,
    )

    db.commit()
    db.refresh(vendor)
    return vendor


@router.delete("/{vendor_id}")
def delete_vendor(
    vendor_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    audit_service.record(
        db,
        actor=current_user,
        action="vendor.deleted",
        category="Vendors",
        action_type="Deleted",
        entity_type="vendor",
        entity_id=vendor.id,
        entity_name=vendor.name,
        summary=f"Deleted vendor {vendor.name}",
        request=http_request,
    )

    db.delete(vendor)
    db.commit()
    return {"message": "Vendor deleted"}
