"""
WiFi Networks API - CRUD for office WiFi credentials.
Admins can add/edit/delete multiple WiFi networks.
All authenticated users can read them.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.wifi_network import WifiNetwork

router = APIRouter(prefix="/api/wifi-networks", tags=["WiFi Networks"])


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
def create_wifi_network(payload: WifiNetworkCreate, db: Session = Depends(get_db)):
    """Add a new WiFi network (admin-only)."""
    wifi = WifiNetwork(**payload.model_dump())
    db.add(wifi)
    db.commit()
    db.refresh(wifi)
    return wifi


@router.put("/{wifi_id}", response_model=WifiNetworkResponse)
def update_wifi_network(wifi_id: int, payload: WifiNetworkUpdate, db: Session = Depends(get_db)):
    """Update an existing WiFi network (admin-only)."""
    wifi = db.query(WifiNetwork).filter(WifiNetwork.id == wifi_id).first()
    if not wifi:
        raise HTTPException(status_code=404, detail="WiFi network not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(wifi, key, value)
    db.commit()
    db.refresh(wifi)
    return wifi


@router.delete("/{wifi_id}")
def delete_wifi_network(wifi_id: int, db: Session = Depends(get_db)):
    """Delete a WiFi network (admin-only)."""
    wifi = db.query(WifiNetwork).filter(WifiNetwork.id == wifi_id).first()
    if not wifi:
        raise HTTPException(status_code=404, detail="WiFi network not found")
    db.delete(wifi)
    db.commit()
    return {"message": "WiFi network deleted successfully"}
