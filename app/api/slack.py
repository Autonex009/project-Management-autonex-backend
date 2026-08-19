import json
import hmac
import hashlib
import time
import logging
import urllib.request

from fastapi import APIRouter, Request, HTTPException, Depends, Form
from sqlalchemy.orm import Session
from app.db.database import get_db
from app.services.slack_service import get_slack_signing_secret, _slack_request
from app.models.employee import Employee
from app.models.user import User

from app.api.wfh import WFHApproveBody, approve_wfh, reject_wfh
from app.api.leaves import ApproveBody as LeaveApproveBody, approve_leave, reject_leave

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/slack", tags=["Slack"])

@router.post("/interactions")
async def slack_interactions(request: Request, payload: str = Form(...), db: Session = Depends(get_db)):
    # 1. Verify signature
    signing_secret = get_slack_signing_secret()
    if not signing_secret:
        raise HTTPException(status_code=500, detail="Slack signing secret not configured")

    body_bytes = await request.body()
    timestamp = request.headers.get("x-slack-request-timestamp", "")
    slack_signature = request.headers.get("x-slack-signature", "")

    if not timestamp or not slack_signature:
        raise HTTPException(status_code=400, detail="Missing Slack headers")

    if abs(time.time() - int(timestamp)) > 60 * 5:
        raise HTTPException(status_code=400, detail="Invalid timestamp")

    sig_basestring = f"v0:{timestamp}:{body_bytes.decode('utf-8')}"
    my_signature = "v0=" + hmac.new(
        signing_secret.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(my_signature, slack_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 2. Parse payload
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    
    if data.get("type") != "block_actions":
        return {"status": "ignored"}

    actions = data.get("actions", [])
    if not actions:
        return {"status": "ignored"}
        
    action = actions[0]
    
    try:
        action_value = json.loads(action.get("value", "{}"))
    except json.JSONDecodeError:
        return {"status": "ignored"}
        
    action_type = action_value.get("action")
    req_type = action_value.get("type")
    req_id = action_value.get("id")

    if not action_type or not req_type or not req_id:
        return {"status": "ignored"}

    response_url = data.get("response_url")

    # Look up user
    slack_user_id = data.get("user", {}).get("id")
    employee = db.query(Employee).filter(Employee.slack_user_id == slack_user_id).first()
    
    user = db.query(User).filter(User.employee_id == employee.id).first() if employee else None
    if not user:
        _respond_to_slack(response_url, "You are not mapped to an Autonex user. Please contact the administrator.")
        return {"status": "ok"}

    # Execute Approval/Rejection
    try:
        if req_type == "wfh":
            body = WFHApproveBody(remark="")
            if action_type == "approve":
                approve_wfh(wfh_id=req_id, http_request=request, approved_by=0, body=body, db=db, current_user=user)
                _respond_to_slack(response_url, f":white_check_mark: You approved this WFH request.")
            elif action_type == "reject":
                reject_wfh(wfh_id=req_id, http_request=request, approved_by=0, body=body, db=db, current_user=user)
                _respond_to_slack(response_url, f":x: You rejected this WFH request.")
        elif req_type == "leave":
            body = LeaveApproveBody(remark="")
            if action_type == "approve":
                approve_leave(leave_id=req_id, http_request=request, approved_by=0, body=body, db=db, current_user=user)
                _respond_to_slack(response_url, f":white_check_mark: You approved this leave request.")
            elif action_type == "reject":
                reject_leave(leave_id=req_id, http_request=request, approved_by=0, body=body, db=db, current_user=user)
                _respond_to_slack(response_url, f":x: You rejected this leave request.")
    except HTTPException as e:
        _respond_to_slack(response_url, f"Error: {e.detail}")
    except Exception as e:
        logger.error(f"Error processing slack interaction: {e}")
        _respond_to_slack(response_url, f"An unexpected error occurred: {str(e)}")

    return {"status": "ok"}


def _respond_to_slack(response_url: str, text: str):
    if not response_url:
        return
        
    payload = json.dumps({
        "replace_original": False,
        "text": text
    }).encode("utf-8")
    
    req = urllib.request.Request(
        response_url, 
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST"
    )
    
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.error(f"Failed to send response to Slack URL: {e}")
