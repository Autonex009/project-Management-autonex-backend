"""
Chat API router — streaming chat endpoint and action confirmation.
"""
import json
import logging
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.db.database import get_db
from app.models.user import User
from app.models.leave import Leave
from app.models.wfh import WFHRequest
from app.services.auth_service import get_current_user
from app.services.chat_agent import chat_stream, get_chat_history, get_user_conversations
from app.constants.leave_types import normalize_leave_type

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


# ── Schemas ─────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None


class ConfirmLeaveRequest(BaseModel):
    employee_id: int
    leave_type: str
    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD
    reason: str


class ConfirmWFHRequest(BaseModel):
    employee_id: int
    wfh_date: str    # YYYY-MM-DD
    end_date: Optional[str] = None  # YYYY-MM-DD
    reason: str


class CancelLeaveRequest(BaseModel):
    leave_id: int


# ── Streaming Chat Endpoint ────────────────────────────────────────
@router.post("/stream")
async def stream_chat(
    body: ChatRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Streaming chat endpoint using Server-Sent Events (SSE).
    Sends JSON events for tokens, tool calls, confirmations, and completion.
    """
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    conversation_id = body.conversation_id or str(uuid.uuid4())
    employee_id = user.employee_id

    if not employee_id:
        raise HTTPException(
            status_code=400,
            detail="No employee profile linked to your account. Please contact admin.",
        )

    # Determine role from the auth system
    role = user.role or "employee"

    async def event_generator():
        async for event_data in chat_stream(
            message=body.message,
            conversation_id=conversation_id,
            user_id=user.id,
            employee_id=employee_id,
            role=role,
            db=db,
        ):
            yield f"data: {event_data}\n\n"

        # Send conversation_id in the final event
        yield f"data: {json.dumps({'type': 'meta', 'conversation_id': conversation_id})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


# ── Confirm Leave Action ───────────────────────────────────────────
@router.post("/confirm-leave")
def confirm_leave(
    body: ConfirmLeaveRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Execute a confirmed leave application."""
    if user.employee_id != body.employee_id:
        raise HTTPException(status_code=403, detail="You can only apply leave for yourself.")

    try:
        start = date.fromisoformat(body.start_date)
        end = date.fromisoformat(body.end_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format")

    if start < date.today():
        raise HTTPException(status_code=400, detail="Cannot apply leave for past dates")

    leave = Leave(
        employee_id=body.employee_id,
        leave_type=normalize_leave_type(body.leave_type),
        start_date=start,
        end_date=end,
        reason=body.reason,
        status="pending",
    )
    db.add(leave)
    db.commit()
    db.refresh(leave)

    logger.info(
        "Chat: Leave created #%s for employee %s (%s → %s)",
        leave.id, body.employee_id, body.start_date, body.end_date,
    )

    return {
        "success": True,
        "message": f"Leave request #{leave.id} submitted successfully. It is pending approval.",
        "leave_id": leave.id,
    }


# ── Confirm WFH Action ─────────────────────────────────────────────
@router.post("/confirm-wfh")
def confirm_wfh(
    body: ConfirmWFHRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Execute a confirmed WFH application."""
    if user.employee_id != body.employee_id:
        raise HTTPException(status_code=403, detail="You can only apply WFH for yourself.")

    try:
        wfh_start = date.fromisoformat(body.wfh_date)
        wfh_end = date.fromisoformat(body.end_date) if body.end_date else wfh_start
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format")

    if wfh_start < date.today():
        raise HTTPException(status_code=400, detail="Cannot apply WFH for past dates")

    wfh = WFHRequest(
        employee_id=body.employee_id,
        wfh_date=wfh_start,
        end_date=wfh_end,
        reason=body.reason,
        status="pending",
    )
    db.add(wfh)
    db.commit()
    db.refresh(wfh)

    logger.info(
        "Chat: WFH created #%s for employee %s (%s)",
        wfh.id, body.employee_id, body.wfh_date,
    )

    return {
        "success": True,
        "message": f"WFH request #{wfh.id} submitted successfully. It is pending approval.",
        "wfh_id": wfh.id,
    }


# ── Cancel Leave Action ────────────────────────────────────────────
@router.post("/cancel-leave")
def cancel_leave_action(
    body: CancelLeaveRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Execute a confirmed leave cancellation."""
    leave = db.query(Leave).filter(
        Leave.id == body.leave_id,
        Leave.employee_id == user.employee_id,
    ).first()

    if not leave:
        raise HTTPException(status_code=404, detail="Leave request not found")

    if leave.start_date < date.today():
        raise HTTPException(status_code=400, detail="Cannot cancel a past leave")

    db.delete(leave)
    db.commit()

    logger.info("Chat: Leave #%s cancelled for employee %s", body.leave_id, user.employee_id)

    return {
        "success": True,
        "message": f"Leave request #{body.leave_id} has been cancelled.",
    }


# ── Get Conversation History ───────────────────────────────────────
@router.get("/history/{conversation_id}")
def get_history(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retrieve messages for a conversation."""
    messages = get_chat_history(conversation_id, db)
    return {"conversation_id": conversation_id, "messages": messages}


# ── List User Conversations ────────────────────────────────────────
@router.get("/conversations")
def list_conversations(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all conversations for the current user."""
    conversations = get_user_conversations(user.id, db)
    return {"conversations": conversations}
