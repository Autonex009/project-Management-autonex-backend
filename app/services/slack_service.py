import json
import logging
import os
import asyncio
from datetime import datetime
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"


def _get_bot_token() -> str | None:
    return os.getenv("SLACK_BOT_TOKEN")


def get_slack_signing_secret() -> str | None:
    return os.getenv("SLACK_SIGNING_SECRET")


def _slack_request(path: str, payload: dict | None = None, method: str = "POST", use_json: bool = True) -> dict:
    token = _get_bot_token()
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN is not configured")

    payload = payload or {}
    request_url = f"{SLACK_API_BASE}{path}"
    data = None
    headers = {
        "Authorization": f"Bearer {token}",
    }

    if method.upper() == "GET":
        if payload:
            request_url = f"{request_url}?{urlencode(payload)}"
    elif use_json:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    else:
        data = urlencode(payload).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    request = Request(
        request_url,
        data=data,
        headers=headers,
        method=method.upper(),
    )

    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore") or exc.reason
        raise RuntimeError(f"Slack API request failed: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Slack API request failed: {exc.reason}") from exc


def lookup_user_id_by_email(email: str) -> str | None:
    response = _slack_request("/users.lookupByEmail", {"email": email}, method="GET")
    if response.get("ok"):
        return response.get("user", {}).get("id")

    error_code = response.get("error")
    if error_code == "users_not_found":
        logger.warning("Slack lookup skipped for %s: %s", email, error_code)
        return None

    raise RuntimeError(f"Slack lookup failed: {error_code or 'unknown_error'}")


def get_employee_slack_email(employee) -> str | None:
    # The employee email field stores the Slack email used for Slack-integrated notifications.
    return getattr(employee, "email", None)


def lookup_user_avatar_by_email(email: str) -> str | None:
    """Return the best-resolution profile image URL for a Slack user, by email."""
    response = _slack_request("/users.lookupByEmail", {"email": email}, method="GET")
    if not response.get("ok"):
        error_code = response.get("error")
        if error_code == "users_not_found":
            logger.warning("Slack avatar lookup skipped for %s: %s", email, error_code)
            return None
        raise RuntimeError(f"Slack lookup failed: {error_code or 'unknown_error'}")

    profile = response.get("user", {}).get("profile", {}) or {}
    # Prefer the largest available image; fall back through the size variants.
    for key in ("image_original", "image_512", "image_192", "image_72", "image_48"):
        url = profile.get(key)
        if url:
            return url
    return None


def try_lookup_user_avatar_by_email(email: str) -> str | None:
    try:
        return lookup_user_avatar_by_email(email)
    except Exception as exc:
        logger.warning("Slack avatar lookup skipped for %s: %s", email or "unknown", exc)
        return None


def get_or_cache_employee_slack_user_id(db, employee) -> str | None:
    if getattr(employee, "slack_user_id", None):
        return employee.slack_user_id

    slack_email = get_employee_slack_email(employee)
    if not slack_email:
        return None

    user_id = lookup_user_id_by_email(slack_email)
    if not user_id:
        return None

    employee.slack_user_id = user_id
    db.add(employee)
    db.commit()
    db.refresh(employee)
    return user_id


def try_get_or_cache_employee_slack_user_id(db, employee) -> str | None:
    try:
        return get_or_cache_employee_slack_user_id(db, employee)
    except Exception as exc:
        logger.warning("Slack user lookup/cache skipped for %s: %s", get_employee_slack_email(employee) or "unknown", exc)
        return None


def open_direct_message_channel(user_id: str) -> str:
    response = _slack_request("/conversations.open", {"users": user_id})
    if response.get("ok"):
        channel_id = response.get("channel", {}).get("id")
        if channel_id:
            return channel_id

    raise RuntimeError(f"Slack DM open failed: {response.get('error') or 'unknown_error'}")


def send_leave_applied_message(*, employee_name: str, employee_email: str, leave_type: str, start_date: str, end_date: str) -> bool:
    user_id = lookup_user_id_by_email(employee_email)
    if not user_id:
        return False
    channel_id = open_direct_message_channel(user_id)

    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": "You applied for leave.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*You applied for leave.*\nYour leave request has been recorded in Autonex.",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Employee*\n{employee_name}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Leave Type*\n{leave_type}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Start Date*\n{start_date}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*End Date*\n{end_date}",
                        },
                    ],
                },
            ],
        },
    )

    if response.get("ok"):
        return True

    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def try_send_leave_applied_message(**kwargs) -> bool:
    try:
        return send_leave_applied_message(**kwargs)
    except Exception as exc:
        logger.warning("Slack leave notification skipped: %s", exc)
        return False


def get_leave_balances_text(db, employee) -> str:
    from app.constants.leave_types import is_intern_or_contractor
    from app.models.leave import Leave
    from datetime import date, timedelta
    
    is_contractor = is_intern_or_contractor(employee.employee_type)
    current_year = date.today().year
    current_month = date.today().month
    
    leaves = db.query(Leave).filter(
        Leave.employee_id == employee.id,
        Leave.status != "rejected"
    ).all()

    def calc_days(l: Leave):
        if not getattr(l, "start_date", None) or not getattr(l, "end_date", None):
            return 0
        if getattr(l, "is_half_day", False) or l.leave_type in ("first_half", "second_half"):
            return 0.5
        d = 0
        curr = l.start_date
        while curr <= l.end_date:
            if curr.weekday() < 5:
                d += 1
            curr += timedelta(days=1)
        return d

    used_this_month = 0
    used_cl = 0
    used_floater = 0
    used_paid = 0

    for l in leaves:
        if not getattr(l, "start_date", None):
            continue
        if l.start_date.year != current_year:
            continue
        
        days = calc_days(l)
        l_type = l.leave_type or ""
        
        if l_type == "casual_sick":
            used_cl += days
        elif l_type == "floater":
            used_floater += days
        else:
            used_paid += days
            if l.start_date.month == current_month:
                used_this_month += days

    if is_contractor:
        return (
            f"• *Monthly Leave:* 1 / 1 this month ({used_this_month} used)\n"
            f"• *Casual/Sick Leave:* 0 / 0 days left ({used_cl} used in {current_year})\n"
            f"• *Floater Leave:* 2 / 2 days left ({used_floater} used in {current_year})"
        )
    else:
        paid_left = max(0, 12 - used_paid)
        cl_left = max(0, 6 - used_cl)
        floater_left = max(0, 2 - used_floater)
        return (
            f"• *Paid Leave:* {paid_left} / 12 days left ({used_paid} used in {current_year})\n"
            f"• *Casual/Sick Leave:* {cl_left} / 6 days left ({used_cl} used in {current_year})\n"
            f"• *Floater Leave:* {floater_left} / 2 days left ({used_floater} used in {current_year})"
        )

def get_wfh_balances_text(db, employee) -> str:
    from app.models.wfh import WFHRequest
    from datetime import date, timedelta
    from app.constants.leave_types import is_intern_or_contractor
    
    is_contractor = is_intern_or_contractor(employee.employee_type)
    today = date.today()
    current_year = today.year
    current_month = today.month
    
    # Calculate Monday of the current week
    current_monday = today - timedelta(days=today.weekday())
    
    wfhs = db.query(WFHRequest).filter(
        WFHRequest.employee_id == employee.id,
        WFHRequest.status != "rejected"
    ).all()
    
    used_this_month = 0
    used_this_week = 0
    
    for r in wfhs:
        r_end = r.end_date or r.wfh_date
        curr_d = r.wfh_date
        while curr_d <= r_end:
            if curr_d.weekday() < 5:
                if curr_d.year == current_year and curr_d.month == current_month:
                    used_this_month += 1
                r_monday = curr_d - timedelta(days=curr_d.weekday())
                if r_monday == current_monday:
                    used_this_week += 1
            curr_d += timedelta(days=1)

    if is_contractor:
        return f"• *Monthly:* {used_this_month} / 2 this month ({used_this_month} used)"
    else:
        return (
            f"• *Monthly:* {used_this_month} / 4 this month ({used_this_month} used)\n"
            f"• *Weekly:* {used_this_week} / 1 this week ({used_this_week} used)"
        )


def build_leave_request_blocks(
    pm_name: str,
    employee_name: str,
    employee_email: str,
    employee_designation: str | None,
    leave_type: str,
    start_date: str,
    end_date: str,
    duration_days: int,
    reason: str | None,
    impacted_projects: list[str] | None,
    leave_id: int,
    exceeds_limit: bool,
    leave_balances_text: str | None,
    exceeds_limit_text: str | None,
) -> tuple[str, list]:
    projects_text = ", ".join(impacted_projects) if impacted_projects else "No active project mapping found"
    normalized_reason = reason.strip() if isinstance(reason, str) and reason.strip() else "No reason provided"

    designation_text = employee_designation or "Employee"
    def fmt_dt(ds):
        try:
            return datetime.strptime(ds, "%Y-%m-%d").strftime("%b %d, %Y")
        except:
            return ds

    if start_date == end_date:
        date_str = f"{start_date} ({fmt_dt(start_date)})"
    else:
        date_str = f"{start_date} to {end_date} ({fmt_dt(start_date)} to {fmt_dt(end_date)})"
    
    full_text = f"*New {leave_type} Request: {employee_name}* ({designation_text})"
    if exceeds_limit:
        warning_msg = exceeds_limit_text or "Approval requires a mandatory remark."
        full_text += f"\n⚠️ *Limit Exceeded:* {warning_msg}"

    full_text += f"\n\n*Request Details*\n• *Duration:* {duration_days} Day(s) ({date_str})\n• *Reason:* {normalized_reason}\n• *Projects Impacted:* {projects_text}"

    if leave_balances_text:
        full_text += f"\n\n*Leave Balances*\n{leave_balances_text}"

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": full_text,
            },
        }
    ]

    pm_portal_url = (os.getenv("FRONTEND_URL") or "http://localhost:5173").strip().rstrip("/") + "/pm/leaves"

    elements = []
    
    if not exceeds_limit:
        elements.extend([
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "Approve",
                    "emoji": True
                },
                "style": "primary",
                "value": json.dumps({"action": "approve", "type": "leave", "id": leave_id}),
                "action_id": "approve_leave"
            },
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "Reject",
                    "emoji": True
                },
                "style": "danger",
                "value": json.dumps({"action": "reject", "type": "leave", "id": leave_id}),
                "action_id": "reject_leave"
            }
        ])
    
    elements.append({
        "type": "button",
        "text": {
            "type": "plain_text",
            "text": "Review in PM Portal",
            "emoji": True
        },
        "url": pm_portal_url,
        "action_id": "review_in_portal"
    })

    blocks.append({
        "type": "actions",
        "elements": elements
    })

    text = f"New leave request from {employee_name} ({start_date} to {end_date})"
    return text, blocks

def send_pm_leave_request_message(
    *,
    pm_slack_user_id: str,
    pm_name: str,
    employee_name: str,
    employee_email: str,
    employee_designation: str | None,
    leave_type: str,
    start_date: str,
    end_date: str,
    duration_days: int,
    reason: str | None,
    impacted_projects: list[str] | None = None,
    leave_id: int,
    exceeds_limit: bool = False,
    leave_balances_text: str | None = None,
    exceeds_limit_text: str | None = None,
) -> tuple[str | None, str | None]:
    channel_id = open_direct_message_channel(pm_slack_user_id)
    text, blocks = build_leave_request_blocks(
        pm_name, employee_name, employee_email, employee_designation,
        leave_type, start_date, end_date, duration_days, reason, impacted_projects,
        leave_id, exceeds_limit, leave_balances_text, exceeds_limit_text
    )

    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"New leave request from {employee_name} ({start_date} to {end_date})",
            "blocks": blocks,
        },
    )

    if response.get("ok"):
        return (response.get("ts"), channel_id)
    
    logger.error(f"Failed to send PM leave request DM: {response.get('error')}")
    return (None, None)

def try_update_pm_leave_request_message(
    *,
    ts: str,
    channel_id: str,
    pm_name: str,
    employee_name: str,
    employee_email: str,
    employee_designation: str | None,
    leave_type: str,
    start_date: str,
    end_date: str,
    duration_days: int,
    reason: str | None,
    impacted_projects: list[str] | None = None,
    leave_id: int,
    exceeds_limit: bool = False,
    leave_balances_text: str | None = None,
    exceeds_limit_text: str | None = None,
) -> bool:
    try:
        text, blocks = build_leave_request_blocks(
            pm_name, employee_name, employee_email, employee_designation,
            leave_type, start_date, end_date, duration_days, reason, impacted_projects,
            leave_id, exceeds_limit, leave_balances_text, exceeds_limit_text
        )
        response = _slack_request(
            "/chat.update",
            {
                "channel": channel_id,
                "ts": ts,
                "text": text,
                "blocks": blocks,
            },
        )
        if response.get("ok"):
            return True
        logger.error(f"Failed to update PM leave request DM: {response.get('error')}")
        return False
    except Exception as e:
        logger.error(f"Error in try_update_pm_leave_request_message: {e}")
        return False


def try_send_pm_leave_request_message(*args, **kwargs) -> tuple[str | None, str | None]:
    try:
        return send_pm_leave_request_message(*args, **kwargs)
    except Exception as e:
        logger.error(f"Error in send_pm_leave_request_message: {e}")
        return (None, None)



def build_wfh_request_blocks(
    pm_name: str,
    employee_name: str,
    employee_email: str,
    employee_designation: str | None,
    start_date: str,
    end_date: str,
    duration_days: int,
    reason: str | None,
    impacted_projects: list[str] | None,
    wfh_id: int,
    exceeds_limit: bool,
    wfh_balances_text: str | None,
    exceeds_limit_text: str | None,
) -> tuple[str, list]:
    projects_text = ", ".join(impacted_projects) if impacted_projects else "No active project mapping found"
    normalized_reason = reason.strip() if isinstance(reason, str) and reason.strip() else "No reason provided"

    designation_text = employee_designation or "Employee"
    def fmt_dt(ds):
        try:
            return datetime.strptime(ds, "%Y-%m-%d").strftime("%b %d, %Y")
        except:
            return ds

    if start_date == end_date:
        date_str = f"{start_date} ({fmt_dt(start_date)})"
    else:
        date_str = f"{start_date} to {end_date} ({fmt_dt(start_date)} to {fmt_dt(end_date)})"
    
    full_text = f"*New WFH Request: {employee_name}* ({designation_text})"
    if exceeds_limit:
        warning_msg = exceeds_limit_text or "Approval requires a mandatory remark."
        full_text += f"\n⚠️ *Limit Exceeded:* {warning_msg}"

    full_text += f"\n\n*Request Details*\n• *Duration:* {duration_days} Day(s) ({date_str})\n• *Reason:* {normalized_reason}\n• *Projects Impacted:* {projects_text}"

    if wfh_balances_text:
        full_text += f"\n\n*WFH Balances*\n{wfh_balances_text}"

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": full_text,
            },
        }
    ]

    pm_portal_url = (os.getenv("FRONTEND_URL") or "http://localhost:5173").strip().rstrip("/") + "/pm/leaves"

    elements = []
    
    if not exceeds_limit:
        elements.extend([
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "Approve",
                    "emoji": True
                },
                "style": "primary",
                "value": json.dumps({"action": "approve", "type": "wfh", "id": wfh_id}),
                "action_id": "approve_wfh"
            },
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "Reject",
                    "emoji": True
                },
                "style": "danger",
                "value": json.dumps({"action": "reject", "type": "wfh", "id": wfh_id}),
                "action_id": "reject_wfh"
            }
        ])
    
    elements.append({
        "type": "button",
        "text": {
            "type": "plain_text",
            "text": "Review in PM Portal",
            "emoji": True
        },
        "url": pm_portal_url,
        "action_id": "review_in_portal_wfh"
    })

    blocks.append({
        "type": "actions",
        "elements": elements
    })

    text = f"New WFH request from {employee_name} ({start_date} to {end_date})"
    return text, blocks

def try_update_pm_wfh_request_message(
    *,
    ts: str,
    channel_id: str,
    pm_name: str,
    employee_name: str,
    employee_email: str,
    employee_designation: str | None,
    start_date: str,
    end_date: str,
    duration_days: int,
    reason: str | None,
    impacted_projects: list[str] | None = None,
    wfh_id: int,
    exceeds_limit: bool = False,
    wfh_balances_text: str | None = None,
    exceeds_limit_text: str | None = None,
) -> bool:
    try:
        text, blocks = build_wfh_request_blocks(
            pm_name, employee_name, employee_email, employee_designation,
            start_date, end_date, duration_days, reason, impacted_projects,
            wfh_id, exceeds_limit, wfh_balances_text, exceeds_limit_text
        )
        response = _slack_request(
            "/chat.update",
            {
                "channel": channel_id,
                "ts": ts,
                "text": text,
                "blocks": blocks,
            },
        )
        if response.get("ok"):
            return True
        logger.error(f"Failed to update PM WFH request DM: {response.get('error')}")
        return False
    except Exception as e:
        logger.error(f"Error in try_update_pm_wfh_request_message: {e}")
        return False

def send_pm_wfh_request_message(
    *,
    pm_slack_user_id: str,
    pm_name: str,
    employee_name: str,
    employee_email: str,
    employee_designation: str | None,
    start_date: str,
    end_date: str,
    duration_days: int,
    reason: str | None,
    impacted_projects: list[str] | None = None,
    wfh_id: int,
    exceeds_limit: bool = False,
    wfh_balances_text: str | None = None,
    exceeds_limit_text: str | None = None,
) -> tuple[str | None, str | None]:
    channel_id = open_direct_message_channel(pm_slack_user_id)
    text, blocks = build_wfh_request_blocks(
        pm_name, employee_name, employee_email, employee_designation,
        start_date, end_date, duration_days, reason, impacted_projects,
        wfh_id, exceeds_limit, wfh_balances_text, exceeds_limit_text
    )

    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": text,
            "blocks": blocks,
        },
    )

    if response.get("ok"):
        return (response.get("ts"), channel_id)
    
    logger.error(f"Failed to send PM WFH request DM: {response.get('error')}")
    return (None, None)

def try_send_pm_wfh_request_message(*args, **kwargs) -> tuple[str | None, str | None]:
    try:
        return send_pm_wfh_request_message(*args, **kwargs)
    except Exception as e:
        logger.error(f"Error in send_pm_wfh_request_message: {e}")
        return (None, None)


def send_leave_status_message(*, employee_email: str, employee_name: str, start_date: str, end_date: str, pm_name: str, approved: bool) -> bool:
    user_id = lookup_user_id_by_email(employee_email)
    if not user_id:
        return False
    channel_id = open_direct_message_channel(user_id)

    if approved:
        plain_text = f"Leave Approved: Your leave request from {start_date} to {end_date} has been approved by {pm_name}."
        headline = f":white_check_mark: Leave Approved: Your leave request from {start_date} to {end_date} has been approved by {pm_name}."
        status_label = "Approved"
        status_emoji = ":white_check_mark:"
    else:
        plain_text = f"Leave Update: Your leave request from {start_date} to {end_date} has been declined by {pm_name}. Please reach out to them for more details."
        headline = f":x: Leave Update: Your leave request from {start_date} to {end_date} has been declined by {pm_name}. Please reach out to them for more details."
        status_label = "Declined"
        status_emoji = ":x:"

    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": plain_text,
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": headline,
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Employee*\n{employee_name}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Status*\n{status_emoji} {status_label}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Start Date*\n{start_date}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*End Date*\n{end_date}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Approved By*\n{pm_name}",
                        },
                    ],
                },
            ],
        },
    )

    if response.get("ok"):
        return True

    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def try_send_leave_status_message(**kwargs) -> bool:
    try:
        return send_leave_status_message(**kwargs)
    except Exception as exc:
        logger.warning("Slack leave status notification skipped: %s", exc)
        return False


def send_wfh_status_message(*, employee_email: str, employee_name: str, start_date: str, end_date: str, pm_name: str, approved: bool) -> bool:
    user_id = lookup_user_id_by_email(employee_email)
    if not user_id:
        return False
    channel_id = open_direct_message_channel(user_id)

    if approved:
        plain_text = f"WFH Approved: Your WFH request from {start_date} to {end_date} has been approved by {pm_name}."
        headline = f":white_check_mark: WFH Approved: Your WFH request from {start_date} to {end_date} has been approved by {pm_name}."
        status_label = "Approved"
        status_emoji = ":white_check_mark:"
    else:
        plain_text = f"WFH Update: Your WFH request from {start_date} to {end_date} has been declined by {pm_name}. Please reach out to them for more details."
        headline = f":x: WFH Update: Your WFH request from {start_date} to {end_date} has been declined by {pm_name}. Please reach out to them for more details."
        status_label = "Declined"
        status_emoji = ":x:"

    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": plain_text,
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": headline,
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Employee*\n{employee_name}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Status*\n{status_emoji} {status_label}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Start Date*\n{start_date}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*End Date*\n{end_date}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Approved By*\n{pm_name}",
                        },
                    ],
                },
            ],
        },
    )

    if response.get("ok"):
        return True

    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def try_send_wfh_status_message(**kwargs) -> bool:
    try:
        return send_wfh_status_message(**kwargs)
    except Exception as exc:
        logger.warning("Slack WFH status notification skipped: %s", exc)
        return False


def notify_employee_side_project_created(employee, side_project) -> bool:
    user_id = getattr(employee, "slack_user_id", None)
    if not user_id:
        raise RuntimeError("Slack user id is required for this helper")
    channel_id = open_direct_message_channel(user_id)

    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": "Your side project was created.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Your side project was created.*\nA new side project has been added in Autonex.",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Project*\n{side_project.name}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Status*\n{side_project.status}",
                        },
                    ],
                },
            ],
        },
    )

    if response.get("ok"):
        return True

    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def notify_pm_side_project_created(
    *,
    pm_slack_user_id: str,
    pm_name: str,
    employee_name: str,
    employee_email: str,
    employee_designation: str | None,
    side_project_name: str,
    side_project_description: str | None,
    side_project_status: str,
    start_date: str | None,
    end_date: str | None,
    impacted_projects: list[str] | None = None,
) -> bool:
    channel_id = open_direct_message_channel(pm_slack_user_id)
    project_lines = impacted_projects or ["No active project mapping found"]
    projects_text = "\n".join(f"• {line}" for line in project_lines)
    description_text = side_project_description.strip() if isinstance(side_project_description, str) and side_project_description.strip() else "No description provided"

    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"New side project created by {employee_name}: {side_project_name}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*New employee side project created*\n{employee_name} created a side project in Autonex.",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*PM*\n{pm_name}"},
                        {"type": "mrkdwn", "text": f"*Employee*\n{employee_name}"},
                        {"type": "mrkdwn", "text": f"*Email*\n{employee_email}"},
                        {"type": "mrkdwn", "text": f"*Designation*\n{employee_designation or 'N/A'}"},
                        {"type": "mrkdwn", "text": f"*Side Project*\n{side_project_name}"},
                        {"type": "mrkdwn", "text": f"*Status*\n{side_project_status}"},
                        {"type": "mrkdwn", "text": f"*Start Date*\n{start_date or 'N/A'}"},
                        {"type": "mrkdwn", "text": f"*End Date*\n{end_date or 'N/A'}"},
                    ],
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Description*\n{description_text}",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Employee's Active Project Context*\n{projects_text}",
                    },
                },
            ],
        },
    )

    if response.get("ok"):
        return True

    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def notify_pm_side_project_deleted(
    *,
    pm_slack_user_id: str,
    pm_name: str,
    employee_name: str,
    employee_email: str,
    employee_designation: str | None,
    side_project_name: str,
    side_project_description: str | None,
    side_project_status: str,
    start_date: str | None,
    end_date: str | None,
    impacted_projects: list[str] | None = None,
) -> bool:
    channel_id = open_direct_message_channel(pm_slack_user_id)
    project_lines = impacted_projects or ["No active project mapping found"]
    projects_text = "\n".join(f"• {line}" for line in project_lines)
    description_text = side_project_description.strip() if isinstance(side_project_description, str) and side_project_description.strip() else "No description provided"

    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"Side project deleted by {employee_name}: {side_project_name}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Employee side project deleted*\n{employee_name} deleted a side project in Autonex.",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*PM*\n{pm_name}"},
                        {"type": "mrkdwn", "text": f"*Employee*\n{employee_name}"},
                        {"type": "mrkdwn", "text": f"*Email*\n{employee_email}"},
                        {"type": "mrkdwn", "text": f"*Designation*\n{employee_designation or 'N/A'}"},
                        {"type": "mrkdwn", "text": f"*Side Project*\n{side_project_name}"},
                        {"type": "mrkdwn", "text": f"*Last Known Status*\n{side_project_status}"},
                        {"type": "mrkdwn", "text": f"*Start Date*\n{start_date or 'N/A'}"},
                        {"type": "mrkdwn", "text": f"*End Date*\n{end_date or 'N/A'}"},
                    ],
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Description*\n{description_text}",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Employee's Active Project Context*\n{projects_text}",
                    },
                },
            ],
        },
    )

    if response.get("ok"):
        return True

    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def notify_employee_allocation_created(
    *,
    employee_slack_user_id: str,
    employee_name: str,
    sub_project_name: str,
    project_manager_name: str,
    avg_time_per_task: str,
    target_tasks_per_employee: str,
    timeline: str,
    allocated_hours_per_day: str,
    role_tags: list[str] | None = None,
) -> bool:
    channel_id = open_direct_message_channel(employee_slack_user_id)
    roles_text = ", ".join(role_tags or []) or "No role tags"

    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"You have been allocated to sub-project {sub_project_name}.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*New allocation assigned*\n{employee_name}, you have been allocated to a sub-project in Autonex.",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Sub-Project*\n{sub_project_name}"},
                        {"type": "mrkdwn", "text": f"*Project Manager*\n{project_manager_name}"},
                        {"type": "mrkdwn", "text": f"*Avg Time*\n{avg_time_per_task}"},
                        {"type": "mrkdwn", "text": f"*Your Target (Tasks/Emp)*\n{target_tasks_per_employee}"},
                        {"type": "mrkdwn", "text": f"*Timeline*\n{timeline}"},
                        {"type": "mrkdwn", "text": f"*Allocated Hours/Day*\n{allocated_hours_per_day}"},
                    ],
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Role Tags*\n{roles_text}",
                    },
                },
            ],
        },
    )

    if response.get("ok"):
        return True

    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def notify_employee_allocation_removed(
    *,
    employee_slack_user_id: str,
    employee_name: str,
    sub_project_name: str,
    project_manager_name: str,
    timeline: str,
    allocated_hours_per_day: str,
    role_tags: list[str] | None = None,
) -> bool:
    channel_id = open_direct_message_channel(employee_slack_user_id)
    roles_text = ", ".join(role_tags or []) or "No role tags"

    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"You have been removed from sub-project {sub_project_name}.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Allocation removed*\n{employee_name}, you have been removed from a sub-project in Autonex.",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Sub-Project*\n{sub_project_name}"},
                        {"type": "mrkdwn", "text": f"*Project Manager*\n{project_manager_name}"},
                        {"type": "mrkdwn", "text": f"*Timeline*\n{timeline}"},
                        {"type": "mrkdwn", "text": f"*Previous Allocated Hours/Day*\n{allocated_hours_per_day}"},
                    ],
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Role Tags*\n{roles_text}",
                    },
                },
            ],
        },
    )

    if response.get("ok"):
        return True

    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def notify_employee_sub_project_updated(
    *,
    employee_slack_user_id: str,
    employee_name: str,
    sub_project_name: str,
    project_manager_name: str,
    avg_time_per_task: str,
    target_tasks_per_employee: str,
    timeline: str,
    status: str,
    changes_summary: str,
) -> bool:
    channel_id = open_direct_message_channel(employee_slack_user_id)

    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"Sub-project {sub_project_name} has been updated.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Sub-project updated*\n{employee_name}, a sub-project you are allocated to has been updated in Autonex.",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Sub-Project*\n{sub_project_name}"},
                        {"type": "mrkdwn", "text": f"*Project Manager*\n{project_manager_name}"},
                        {"type": "mrkdwn", "text": f"*Avg Time*\n{avg_time_per_task}"},
                        {"type": "mrkdwn", "text": f"*Your Target (Tasks/Emp)*\n{target_tasks_per_employee}"},
                        {"type": "mrkdwn", "text": f"*Timeline*\n{timeline}"},
                        {"type": "mrkdwn", "text": f"*Status*\n{status}"},
                    ],
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*What Changed*\n{changes_summary}",
                    },
                },
            ],
        },
    )

    if response.get("ok"):
        return True

    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def send_password_reset_message(employee_email: str, reset_link: str) -> bool:
    """Send a password reset link to employee via Slack DM."""
    user_id = lookup_user_id_by_email(employee_email)
    if not user_id:
        raise RuntimeError(f"Slack user not found for email: {employee_email}")
    
    channel_id = open_direct_message_channel(user_id)
    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"Click here to reset your Autonex password: {reset_link}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Reset your Autonex password*\nWe received a password reset request for your account.",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Reset Password",
                            },
                            "style": "primary",
                            "url": reset_link,
                        }
                    ],
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "This link expires in 15 minutes.",
                        },
                    ],
                },
            ],
        },
    )

    if response.get("ok"):
        return True

    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def try_send_password_reset_message(employee_email: str, reset_link: str) -> bool:
    """Attempt to send a password reset link. Returns True if successful, False otherwise."""
    try:
        return send_password_reset_message(employee_email=employee_email, reset_link=reset_link)
    except Exception as exc:
        logger.warning("Slack password reset notification skipped for %s: %s", employee_email, exc)
        return False


def _checkin_frontend_url() -> str:
    base = (os.getenv("FRONTEND_URL") or "http://localhost:5173").strip().rstrip("/")
    return f"{base}/employee/dashboard"


def send_checkin_reminder_message(*, employee_slack_user_id: str, employee_name: str) -> bool:
    """DM an employee who hasn't checked in yet, with a link back to the dashboard
    (the check-in modal shows itself there — see DailyCheckInModal)."""
    channel_id = open_direct_message_channel(employee_slack_user_id)
    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"Good morning {employee_name} — don't forget to check in on Autonex today.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Good morning, {employee_name}!*\nYou haven't checked in yet today — mark your attendance, work mode, and today's project(s) on Autonex.",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Check In Now"},
                            "style": "primary",
                            "url": _checkin_frontend_url(),
                        }
                    ],
                },
            ],
        },
    )
    if response.get("ok"):
        return True
    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def try_send_checkin_reminder_message(**kwargs) -> bool:
    try:
        return send_checkin_reminder_message(**kwargs)
    except Exception as exc:
        logger.warning("Slack check-in reminder skipped for %s: %s", kwargs.get("employee_name"), exc)
        return False


def send_late_warning_message(*, employee_slack_user_id: str, employee_name: str) -> bool:
    """DM an employee who hasn't checked in yet, warning them of escalation."""
    channel_id = open_direct_message_channel(employee_slack_user_id)
    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"Warning: {employee_name}, you haven't checked in yet.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"Hi {employee_name}, you haven't checked in yet. If you do not check in now, your name will be added to the late check-in list and sent to the admins in a few minutes. Please check in immediately.",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Check In Now"},
                            "style": "danger",
                            "url": _checkin_frontend_url(),
                        }
                    ],
                },
            ],
        },
    )
    if response.get("ok"):
        return True
    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def try_send_late_warning_message(**kwargs) -> bool:
    try:
        return send_late_warning_message(**kwargs)
    except Exception as exc:
        logger.warning("Slack late check-in warning skipped for %s: %s", kwargs.get("employee_name"), exc)
        return False


def send_admin_late_list(*, channel_id: str, late_checkins: list[tuple[str, str, str]], pending_checkins: list[tuple[str, str]]) -> bool:
    """Send the late check-in report to a specific channel (e.g. admins)."""
    if not late_checkins and not pending_checkins:
        return True

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Daily Late Check-in Report (Late: {len(late_checkins)} | Pending: {len(pending_checkins)})",
                "emoji": True
            }
        }
    ]

    def make_cell(text):
        return {"type": "raw_text", "text": str(text)}

    def add_table_section(title, employee_list, is_late_list=False):
        if not employee_list:
            return
        
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{title} ({len(employee_list)})*"
            }
        })
        
        chunk_size = 50
        for i in range(0, len(employee_list), chunk_size):
            chunk = employee_list[i:i + chunk_size]
            
            if is_late_list:
                rows = [[make_cell("S.No"), make_cell("Employee Name"), make_cell("Email"), make_cell("Time")]]
                for idx, record in enumerate(chunk):
                    name, email, time_str = record
                    rows.append([make_cell(i + idx + 1), make_cell(name), make_cell(email), make_cell(time_str)])
                
                blocks.append({
                    "type": "table",
                    "rows": rows,
                    "column_settings": [
                        {"align": "center"},
                        {"align": "left", "is_wrapped": True},
                        {"align": "left", "is_wrapped": True},
                        {"align": "right"}
                    ]
                })
            else:
                rows = [[make_cell("S.No"), make_cell("Employee Name"), make_cell("Email")]]
                for idx, record in enumerate(chunk):
                    name, email = record
                    rows.append([make_cell(i + idx + 1), make_cell(name), make_cell(email)])

                blocks.append({
                    "type": "table",
                    "rows": rows,
                    "column_settings": [
                        {"align": "center"},
                        {"align": "left", "is_wrapped": True},
                        {"align": "left", "is_wrapped": True}
                    ]
                })

    add_table_section("Checked In Late (After 11:00 AM)", late_checkins, is_late_list=True)
    add_table_section("Pending (Not Checked In Yet)", pending_checkins, is_late_list=False)

    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"Daily Late Check-in Report (Late: {len(late_checkins)} | Pending: {len(pending_checkins)})",
            "blocks": blocks,
        },
    )
    if response.get("ok"):
        return True
    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def try_send_admin_late_list(**kwargs) -> bool:
    try:
        return send_admin_late_list(**kwargs)
    except Exception as exc:
        logger.warning("Slack admin late list report skipped: %s", exc)
        return False


def send_pm_confirm_reminder_message(*, pm_slack_user_id: str, pm_name: str, pending_count: int) -> bool:
    """DM a PM/lead who still has unconfirmed check-ins on their roster."""
    channel_id = open_direct_message_channel(pm_slack_user_id)
    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"{pm_name}, you have {pending_count} check-in(s) to confirm today.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Team check-ins waiting on you*\n{pm_name}, {pending_count} check-in{'s' if pending_count != 1 else ''} on your team are still unconfirmed for today.",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Review Team"},
                            "style": "primary",
                            "url": (os.getenv("FRONTEND_URL") or "http://localhost:5173").strip().rstrip("/") + "/pm/team-checkins",
                        }
                    ],
                },
            ],
        },
    )
    if response.get("ok"):
        return True
    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def try_send_pm_confirm_reminder_message(**kwargs) -> bool:
    try:
        return send_pm_confirm_reminder_message(**kwargs)
    except Exception as exc:
        logger.warning("Slack PM confirm reminder skipped for %s: %s", kwargs.get("pm_name"), exc)
        return False


async def send_slack_reset_link(user_slack_id: str, reset_link: str) -> None:
    """Send a password reset link to a user's Slack DM without blocking the event loop."""
    await asyncio.to_thread(_send_slack_reset_link_sync, user_slack_id, reset_link)


def _send_slack_reset_link_sync(user_slack_id: str, reset_link: str) -> None:
    channel_id = open_direct_message_channel(user_slack_id)
    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"Click here to reset your Autonex password: {reset_link}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Reset your Autonex password*\nWe received a password reset request for your account.",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Reset Password",
                            },
                            "style": "primary",
                            "url": reset_link,
                        }
                    ],
                },
                {
                    "type": "context",
                    "elements": [
                    
                        {
                            "type": "mrkdwn",
                            "text": "This link expires in 15 minutes.",
                        },
                    ],
                },
            ],
        },
    )

    if response.get("ok"):
        return

    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")

def send_channel_message(channel: str, text: str, blocks: list | None = None) -> bool:
    try:
        payload = {
            "channel": channel,
            "text": text,
        }
        if blocks:
            payload["blocks"] = blocks
            
        response = _slack_request(
            "/chat.postMessage",
            payload
        )
        if not response.get("ok"):
            logger.error(f"Failed to send Slack channel message: {response.get('error')}")
            return False
        return True
    except Exception as e:
        logger.error(f"Exception while sending Slack channel message: {e}")
        return False

def try_overwrite_deleted_message(channel_id: str, ts: str):
    try:
        _slack_request(
            "/chat.update",
            {
                "channel": channel_id,
                "ts": ts,
                "text": "🗑️ This request was deleted by the employee.",
                "blocks": [{
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "🗑️ *This request was deleted by the employee.*"
                    }
                }]
            }
        )
    except Exception as e:
        logger.error(f"Failed to overwrite deleted slack message: {e}")
