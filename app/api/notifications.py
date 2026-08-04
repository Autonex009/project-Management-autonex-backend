"""Notifications API.

Every endpoint here operates on the *caller's own* notifications. The owner is taken
from the authenticated session, never from the request.

The legacy ``user_id`` query parameter is still accepted so existing callers keep
working, but it is ignored: it used to select whose notifications were read, marked or
deleted, which let any authenticated user pass someone else's id and act on their
inbox. Remove the parameter once the frontend stops sending it.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from app.services.auth_service import get_current_user
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

from app.db.deps import get_db
from app.models.notification import Notification
from app.models.user import User

router = APIRouter(prefix="/api/notifications", tags=["notifications"], dependencies=[Depends(get_current_user)])


class NotificationResponse(BaseModel):
    id: int
    user_id: int
    title: str
    message: str
    type: str
    is_read: bool
    created_at: datetime

    class Config:
        from_attributes = True


@router.get("", response_model=List[NotificationResponse])
def get_notifications(
    user_id: Optional[int] = Query(default=None, deprecated=True),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the caller's own 50 most recent notifications."""
    return (
        db.query(Notification)
        .filter(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(50)
        .all()
    )


@router.patch("/{notification_id}/read")
def mark_read(
    notification_id: int,
    user_id: Optional[int] = Query(default=None, deprecated=True),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark one of the caller's own notifications as read."""
    n = db.query(Notification).filter(
        Notification.id == notification_id,
        # Scoping to the session user doubles as the ownership check: another user's
        # notification simply isn't found, so nothing leaks about whether it exists.
        Notification.user_id == current_user.id,
    ).first()
    if not n:
        raise HTTPException(status_code=404, detail="Notification not found")
    n.is_read = True
    db.commit()
    return {"ok": True}


@router.patch("/read-all")
def mark_all_read(
    user_id: Optional[int] = Query(default=None, deprecated=True),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark all of the caller's own unread notifications as read."""
    db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.is_read == False,
    ).update({"is_read": True})
    db.commit()
    return {"ok": True}


@router.delete("/clear-read")
def clear_read_notifications(
    user_id: Optional[int] = Query(default=None, deprecated=True),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete the caller's own already-read notifications."""
    db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.is_read == True,
    ).delete(synchronize_session=False)
    db.commit()
    return {"ok": True}
