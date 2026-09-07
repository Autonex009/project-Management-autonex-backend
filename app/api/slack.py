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
async def slack_interactions(request: Request, db: Session = Depends(get_db)):
    # 1. Verify signature
    signing_secret = get_slack_signing_secret()
    if not signing_secret:
        raise HTTPException(status_code=500, detail="Slack signing secret not configured")

    body_bytes = await request.body()
    form_data = await request.form()
    payload = form_data.get("payload")
    
    if not payload:
        raise HTTPException(status_code=400, detail="Missing payload")
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

    original_blocks = data.get("message", {}).get("blocks", [])
    new_blocks = [b for b in original_blocks if b.get("type") != "actions"]

    def respond_success(action_str: str):
        approver_name = employee.name if employee else "an administrator"
        status_text = f":white_check_mark: {action_str} by {approver_name}" if "approved" in action_str else f":x: {action_str} by {approver_name}"
        
        if new_blocks:
            new_blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": status_text
                }
            })
            _respond_to_slack(response_url, status_text, replace_original=True, blocks=new_blocks)
        else:
            _respond_to_slack(response_url, status_text, replace_original=True)

    # Execute Approval/Rejection
    try:
        if req_type == "wfh":
            body = WFHApproveBody(remark="")
            if action_type == "approve":
                approve_wfh(wfh_id=req_id, http_request=request, approved_by=user.id, body=body, db=db, current_user=user)
                respond_success("WFH request approved")
            elif action_type == "reject":
                reject_wfh(wfh_id=req_id, http_request=request, approved_by=user.id, body=body, db=db, current_user=user)
                respond_success("WFH request rejected")
        elif req_type == "leave":
            body = LeaveApproveBody(remark="")
            if action_type == "approve":
                approve_leave(leave_id=req_id, http_request=request, approved_by=user.id, body=body, db=db, current_user=user)
                respond_success("Leave request approved")
            elif action_type == "reject":
                reject_leave(leave_id=req_id, http_request=request, approved_by=user.id, body=body, db=db, current_user=user)
                respond_success("Leave request rejected")
    except HTTPException as e:
        _respond_to_slack(response_url, f"Error: {e.detail}")
    except Exception as e:
        logger.error(f"Error processing slack interaction: {e}")
        _respond_to_slack(response_url, f"An unexpected error occurred: {str(e)}")

    return {"status": "ok"}


def _respond_to_slack(response_url: str, text: str, replace_original: bool = False, blocks: list = None):
    if not response_url:
        return
        
    payload_dict = {
        "replace_original": replace_original,
        "text": text
    }
    if blocks is not None:
        payload_dict["blocks"] = blocks

    payload = json.dumps(payload_dict).encode("utf-8")
    
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
