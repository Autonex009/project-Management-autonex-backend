from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.services.auth_service import require_role
from app.services import encord_sync_service

router = APIRouter(prefix="/api/encord", tags=["Encord Sync"], dependencies=[Depends(require_role("admin"))])


class SyncRange(BaseModel):
    date_from: Optional[str] = None   # "YYYY-MM-DD"
    date_to: Optional[str] = None     # "YYYY-MM-DD"


@router.get("/preview")
def preview(db: Session = Depends(get_db)):
    """Read-only: which projects would be synced and the default window. No Encord call."""
    return encord_sync_service.preview(db)


@router.post("/sync")
def sync(payload: Optional[SyncRange] = None, db: Session = Depends(get_db)):
    """
    Trigger an Encord pull now. Optional date_from/date_to (YYYY-MM-DD) for a backfill;
    otherwise the previous day is pulled. The same logic runs daily via the scheduler.
    """
    start = end = None
    if payload and payload.date_from:
        try:
            start = datetime.strptime(payload.date_from, "%Y-%m-%d")
            end = (datetime.strptime(payload.date_to, "%Y-%m-%d")
                   if payload.date_to else datetime.now())
        except ValueError:
            raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
    try:
        return encord_sync_service.run_sync(db, start=start, end=end)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
