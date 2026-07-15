from fastapi import APIRouter, Depends, HTTPException
from app.services.auth_service import require_role
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.employee import Employee
from app.models.user import User
from app.services.hiring_sync_service import fetch_hired_candidates, run_sync

router = APIRouter(prefix="/api/hiring", tags=["Hiring Sync"], dependencies=[Depends(require_role("admin"))])


@router.get("/preview")
def preview_hired_candidates(db: Session = Depends(get_db)):
    """Show which hired candidates would be imported — no DB writes."""
    try:
        candidates = fetch_hired_candidates()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    result = []
    for c in candidates:
        if c.get("hr_status") != "hired":
            continue
        email = c.get("email", "")
        already_exists = (
            db.query(Employee).filter(Employee.email == email).first() is not None
            or db.query(User).filter(User.email == email).first() is not None
        )
        result.append({
            "name": c.get("name"),
            "email": email,
            "job_title": c.get("job_title"),
            "job_type": c.get("job_type"),
            "doc_status": c.get("doc_status"),
            "application_id": c.get("application_id"),
            "would_import": not already_exists,
            "skip_reason": "Already exists in PM portal" if already_exists else None,
        })
    return {"total": len(result), "candidates": result}


@router.post("/sync")
def sync_hired_candidates(db: Session = Depends(get_db)):
    """
    Manually trigger the hiring sync.
    The same logic also runs automatically every 12 hours via the background scheduler.
    """
    try:
        return run_sync(db)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
