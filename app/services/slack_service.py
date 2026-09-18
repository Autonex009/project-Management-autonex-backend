import json
import logging
import os
import asyncio
import threading
from datetime import datetime
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"


def _get_bot_token() -> str | None:
    return os.getenv("SLACK_BOT_TOKEN")


def get_slack_signing_secret() -> str | None:
    return os.getenv("SLACK_SIGNING_SECRET")


class SlackRateLimitError(Exception):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Slack rate limit hit. Retry after {retry_after_seconds} seconds.")


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
        if exc.code == 429:
            retry_after = int(exc.headers.get("Retry-After", 10))
            raise SlackRateLimitError(retry_after)
        detail = exc.read().decode("utf-8", errors="ignore") or exc.reason
        raise RuntimeError(f"Slack API request failed: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Slack API request failed: {exc.reason}") from exc


async def _async_slack_request(path: str, payload: dict | None = None, method: str = "POST", use_json: bool = True) -> dict:
    import httpx
    token = _get_bot_token()
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN is not configured")

    payload = payload or {}
    request_url = f"{SLACK_API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
    }

    async with httpx.AsyncClient() as client:
        try:
            if method.upper() == "GET":
                response = await client.get(request_url, params=payload, headers=headers, timeout=30.0)
            elif use_json:
                headers["Content-Type"] = "application/json; charset=utf-8"
                response = await client.post(request_url, json=payload, headers=headers, timeout=30.0)
            else:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                response = await client.post(request_url, data=payload, headers=headers, timeout=30.0)
                
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                retry_after = int(exc.response.headers.get("Retry-After", 10))
                raise SlackRateLimitError(retry_after)
            raise RuntimeError(f"Slack API request failed: {exc.response.text}") from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"Slack API request failed: {str(exc)}") from exc


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


_allocation_pm_batches = {}
_allocation_pm_batches_lock = threading.Lock()


def queue_pm_allocation_batch(
    *,
    pm_slack_user_id: str,
    pm_name: str,
    actor_name: str,
    sub_project_name: str,
    change_type: str,  # "added" | "removed"
    employee_name: str,
    allocated_hours_per_day: str,
    role_tags: list[str] | None = None,
) -> None:
    """
    Queue allocation/deallocation notification for a PM and debounce by 2.5s
    so multiple rapid changes (e.g. bulk allocation modal actions) are sent
    in a single consolidated Slack DM.
    """
    batch_key = (pm_slack_user_id, sub_project_name)

    with _allocation_pm_batches_lock:
        if batch_key not in _allocation_pm_batches:
            _allocation_pm_batches[batch_key] = {
                "pm_slack_user_id": pm_slack_user_id,
                "pm_name": pm_name,
                "actor_name": actor_name,
                "sub_project_name": sub_project_name,
                "added": [],
                "removed": [],
                "timer": None,
            }

        entry = _allocation_pm_batches[batch_key]
        if entry["timer"]:
            entry["timer"].cancel()

        item = {
            "employee_name": employee_name,
            "hours": allocated_hours_per_day,
            "roles": ", ".join(role_tags or []) if role_tags else None,
        }

        if change_type == "added":
            if not any(x["employee_name"] == employee_name for x in entry["added"]):
                entry["added"].append(item)
        elif change_type == "removed":
            if not any(x["employee_name"] == employee_name for x in entry["removed"]):
                entry["removed"].append(item)

        timer = threading.Timer(2.5, _flush_pm_allocation_batch, args=[batch_key])
        entry["timer"] = timer
        timer.start()


def _flush_pm_allocation_batch(batch_key: tuple[str, str]) -> None:
    with _allocation_pm_batches_lock:
        entry = _allocation_pm_batches.pop(batch_key, None)

    if not entry:
        return

    pm_slack_user_id = entry["pm_slack_user_id"]
    actor_name = entry["actor_name"]
    sub_project_name = entry["sub_project_name"]
    added_list = entry["added"]
    removed_list = entry["removed"]

    if not added_list and not removed_list:
        return

    try:
        channel_id = open_direct_message_channel(pm_slack_user_id)

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"📢 *Project Allocation Update by Admin {actor_name}*\nSub-project: *{sub_project_name}*",
                },
            }
        ]

        if added_list:
            items_str = "\n".join(
                f"• *{x['employee_name']}* ({x['hours']}" + (f", Roles: {x['roles']}" if x['roles'] else "") + ")"
                for x in added_list
            )
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"✅ *Newly Allocated ({len(added_list)}):*\n{items_str}",
                }
            })

        if removed_list:
            items_str = "\n".join(
                f"• *{x['employee_name']}*" for x in removed_list
            )
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"❌ *Deallocated ({len(removed_list)}):*\n{items_str}",
                }
            })

        _slack_request(
            "/chat.postMessage",
            {
                "channel": channel_id,
                "text": f"Allocation update for {sub_project_name} by Admin {actor_name}",
                "blocks": blocks,
            },
        )
    except Exception as exc:
        logger.error("Failed to send batched PM allocation notification to %s: %s", pm_slack_user_id, exc)


def notify_project_leader_allocation_created(
    *,
    leader_slack_user_id: str,
    leader_name: str,
    employee_name: str,
    employee_designation: str | None = None,
    sub_project_name: str,
    allocated_hours_per_day: str = "8h/day",
    allocation_source: str = "Auto-allocation (7-day continuous check-in streak)",
    start_date: str | None = None,
    role_tags: list[str] | None = None,
) -> bool:
    channel_id = open_direct_message_channel(leader_slack_user_id)
    roles_text = ", ".join(role_tags or []) or "Standard Member"

    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"New team member {employee_name} allocated to {sub_project_name}.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*New Team Member Allocated*\nHello {leader_name}, *{employee_name}* has been allocated to your project *{sub_project_name}*.",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Sub-Project*\n{sub_project_name}"},
                        {"type": "mrkdwn", "text": f"*Allocated Member*\n{employee_name}"},
                        {"type": "mrkdwn", "text": f"*Designation*\n{employee_designation or 'N/A'}"},
                        {"type": "mrkdwn", "text": f"*Allocated Hours/Day*\n{allocated_hours_per_day}"},
                        {"type": "mrkdwn", "text": f"*Effective Date*\n{start_date or 'Immediate'}"},
                        {"type": "mrkdwn", "text": f"*Allocation Mode*\n{allocation_source}"},
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


def notify_employee_auto_allocated(
    *,
    employee_slack_user_id: str,
    employee_name: str,
    sub_project_name: str,
    allocated_hours_per_day: str = "8h/day",
) -> bool:
    channel_id = open_direct_message_channel(employee_slack_user_id)
    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"You have been permanently allocated to {sub_project_name} after a 7-day continuous check-in streak.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Permanent Allocation Assigned*\nHello {employee_name}, congratulations! You have been permanently allocated to *{sub_project_name}* following a 7-day continuous check-in streak.",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Sub-Project*\n{sub_project_name}"},
                        {"type": "mrkdwn", "text": f"*Allocated Hours/Day*\n{allocated_hours_per_day}"},
                        {"type": "mrkdwn", "text": f"*Status*\nPermanent Team Member"},
                        {"type": "mrkdwn", "text": f"*Trigger*\n7-Day Continuous Check-in Streak"},
                    ],
                },
            ],
        },
    )
    if response.get("ok"):
        return True
    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def send_allocation_notifications_to_leaders(
    db,
    allocation,
    project,
    source: str = "Manual Allocation",
    actor_employee_id: int | None = None,
) -> None:
    """
    Finds all PMs and Team Leads for a project and sends each of them a Slack DM
    notifying them of the new allocation on their project.
    Optimized: Resolves all employees in a single bulk database query.
    """
    if not project or not allocation:
        return

    try:
        from app.models.employee import Employee
        from app.models.sub_project import SubProject as HierarchySubProject
        from app.services.project_scope import project_pm_ids, project_lead_ids

        # 1. Collect all project manager and team lead employee IDs
        leader_ids = project_pm_ids(db, project) | project_lead_ids(db, project)

        if getattr(project, "sub_project_id", None):
            hsp = db.query(HierarchySubProject).filter(HierarchySubProject.id == project.sub_project_id).first()
            if hsp and getattr(hsp, "pm_id", None):
                leader_ids.add(hsp.pm_id)

        # Do not notify the allocated employee themselves if they happen to be in leader_ids
        leader_ids.discard(allocation.employee_id)

        # If an actor_employee_id performed this action, do not send a redundant self-notification
        if actor_employee_id:
            leader_ids.discard(actor_employee_id)

        if not leader_ids:
            logger.info("No PMs or Leads found to notify for project %s (ID %s)", project.name, project.id)
            return

        # 2. Bulk fetch all leaders AND allocated employee in ONE query!
        all_emp_ids = leader_ids | {allocation.employee_id}
        employees = db.query(Employee).filter(Employee.id.in_(all_emp_ids)).all()
        emp_map = {e.id: e for e in employees}

        allocated_emp = emp_map.get(allocation.employee_id)
        if not allocated_emp:
            return

        emp_name = allocated_emp.name
        emp_desig = allocated_emp.designation
        hours_str = f"{allocation.total_daily_hours or 8}h/day"
        start_date_str = str(allocation.active_start_date) if getattr(allocation, "active_start_date", None) else "Immediate"
        role_tags = getattr(allocation, "role_tags", None) or []

        # 3. Notify each leader via Slack
        for leader_id in leader_ids:
            try:
                leader = emp_map.get(leader_id)
                if not leader:
                    continue

                leader_slack_id = try_get_or_cache_employee_slack_user_id(db, leader)
                if not leader_slack_id:
                    logger.debug("Leader %s (ID %s) has no Slack user ID; notification skipped", leader.name, leader_id)
                    continue

                notify_project_leader_allocation_created(
                    leader_slack_user_id=leader_slack_id,
                    leader_name=leader.name,
                    employee_name=emp_name,
                    employee_designation=emp_desig,
                    sub_project_name=project.name,
                    allocated_hours_per_day=hours_str,
                    allocation_source=source,
                    start_date=start_date_str,
                    role_tags=role_tags,
                )
                logger.info("Successfully sent Slack allocation notification to leader %s for project %s", leader.name, project.name)
            except Exception as e:
                logger.warning("Failed to send Slack allocation notification to leader %s: %s", leader_id, e)

    except Exception as exc:
        logger.warning("Error in send_allocation_notifications_to_leaders for project %s: %s", getattr(project, "id", "unknown"), exc)


def notify_project_leader_allocation_removed(
    *,
    leader_slack_user_id: str,
    leader_name: str,
    employee_name: str,
    employee_designation: str | None = None,
    sub_project_name: str,
    remover_name: str,
    remover_role: str,
    allocated_hours_per_day: str = "8h/day",
    role_tags: list[str] | None = None,
) -> bool:
    channel_id = open_direct_message_channel(leader_slack_user_id)
    roles_text = ", ".join(role_tags or []) or "Standard Member"

    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"Team member {employee_name} has been unallocated from {sub_project_name}.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Team Member Allocation Removed*\nHello {leader_name}, *{employee_name}* has been unallocated from your project *{sub_project_name}*.",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Sub-Project*\n{sub_project_name}"},
                        {"type": "mrkdwn", "text": f"*Removed Member*\n{employee_name}"},
                        {"type": "mrkdwn", "text": f"*Designation*\n{employee_designation or 'N/A'}"},
                        {"type": "mrkdwn", "text": f"*Unallocated By*\n{remover_name} ({remover_role})"},
                        {"type": "mrkdwn", "text": f"*Previous Daily Hours*\n{allocated_hours_per_day}"},
                        {"type": "mrkdwn", "text": f"*Role Tags*\n{roles_text}"},
                    ],
                },
            ],
        },
    )

    if response.get("ok"):
        return True

    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def send_unallocation_notifications(
    db,
    alloc_info: dict,
    project_id: int,
    actor_user_role: str | None = None,
    actor_employee_id: int | None = None,
    actor_name: str | None = None,
) -> None:
    """
    Sends Slack notifications when an allocation is removed / unallocated, following the rules:
    - If Admin did it: Notify PM(s), Lead(s), and Employee.
    - If PM did it: Notify Lead(s) and Employee (not the PM).
    - If Lead did it: Notify PM(s) and Employee (not the Lead).
    
    Optimized: Resolves PMs, Leads, and employees using a single batch query.
    """
    try:
        from datetime import date as date_cls
        from app.models.project import DailySheet as Project
        from app.models.parent_project import MainProject
        from app.models.sub_project import SubProject as HierarchySubProject
        from app.models.allocation import Allocation
        from app.models.employee import Employee
        from app.services.slack_service import (
            notify_employee_allocation_removed,
            notify_project_leader_allocation_removed,
            try_get_or_cache_employee_slack_user_id,
        )

        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        # 1. Fast resolve PM candidate IDs from project and parent
        pm_candidate_ids: set[int] = set()
        for v in (project.assigned_employee_ids or []):
            try:
                pm_candidate_ids.add(int(v))
            except (ValueError, TypeError):
                continue

        if not pm_candidate_ids and getattr(project, "main_project_id", None):
            mp = db.query(MainProject).filter(MainProject.id == project.main_project_id).first()
            if mp:
                for v in (getattr(mp, "program_manager_ids", None) or ([mp.program_manager_id] if getattr(mp, "program_manager_id", None) else [])):
                    try:
                        pm_candidate_ids.add(int(v))
                    except (ValueError, TypeError):
                        continue

        if getattr(project, "sub_project_id", None):
            hsp = db.query(HierarchySubProject).filter(HierarchySubProject.id == project.sub_project_id).first()
            if hsp and getattr(hsp, "pm_id", None):
                pm_candidate_ids.add(int(hsp.pm_id))

        # 2. Fast query active project allocations to identify Leads
        lead_candidate_ids: set[int] = set()
        alloc_rows = db.query(Allocation.employee_id, Allocation.role_tags).filter(
            Allocation.sub_project_id == project.id,
            Allocation.is_active == True,
        ).all()
        for eid, role_tags in alloc_rows:
            if eid and any("lead" in str(tag).lower() for tag in (role_tags or [])):
                lead_candidate_ids.add(int(eid))

        # 3. Single bulk query to load all candidate employees in ONE database roundtrip!
        all_candidate_ids = pm_candidate_ids | lead_candidate_ids | {alloc_info["employee_id"]}
        if actor_employee_id:
            all_candidate_ids.add(actor_employee_id)

        employees = db.query(Employee).filter(Employee.id.in_(all_candidate_ids)).all()
        emp_map = {e.id: e for e in employees}

        # Check designations to finalize PMs and Leads
        pm_ids: set[int] = set()
        lead_ids: set[int] = set(lead_candidate_ids)
        for pid in pm_candidate_ids:
            emp = emp_map.get(pid)
            if not emp:
                continue
            desig = (emp.designation or "").strip().lower()
            if "team lead" in desig:
                lead_ids.add(pid)
            else:
                pm_ids.add(pid)

        # 4. Determine Actor Type and Role Label
        role_lower = (actor_user_role or "").strip().lower()
        if role_lower in ("admin", "hr"):
            actor_type = "admin"
            actor_role_label = "Admin"
        elif (actor_employee_id and actor_employee_id in lead_ids and actor_employee_id not in pm_ids) or role_lower == "team_lead":
            actor_type = "lead"
            actor_role_label = "Team Lead"
        else:
            actor_type = "pm"
            actor_role_label = "Project Manager"

        # 5. Apply User Target Rules
        # - If admin: pm/lead and employee
        # - If pm: only to lead and employee
        # - If lead: only to pm and employee
        target_leader_ids: set[int] = set()
        if actor_type == "admin":
            target_leader_ids = pm_ids | lead_ids
        elif actor_type == "pm":
            target_leader_ids = lead_ids
        elif actor_type == "lead":
            target_leader_ids = pm_ids

        # Never notify the actor themselves or the removed employee as a leader
        if actor_employee_id:
            target_leader_ids.discard(actor_employee_id)
        target_leader_ids.discard(alloc_info["employee_id"])

        # 6. Notify Removed Employee
        removed_emp = emp_map.get(alloc_info["employee_id"])
        if removed_emp:
            emp_slack_id = try_get_or_cache_employee_slack_user_id(db, removed_emp)
            if emp_slack_id:
                try:
                    notify_employee_allocation_removed(
                        employee_slack_user_id=emp_slack_id,
                        employee_name=removed_emp.name,
                        sub_project_name=project.name,
                        project_manager_name=actor_name or "Manager",
                        timeline=f"Until {date_cls.today().isoformat()}",
                        allocated_hours_per_day=f"{alloc_info.get('total_daily_hours', 8)}h/day",
                        role_tags=alloc_info.get("role_tags", []),
                    )
                    logger.info("Sent Slack removal notification to employee %s for project %s", removed_emp.name, project.name)
                except Exception as e:
                    logger.warning("Failed to notify removed employee %s: %s", removed_emp.id, e)

        # 7. Notify Target Leaders (PMs and/or Leads based on actor)
        remover_display = actor_name or actor_role_label
        for leader_id in target_leader_ids:
            leader = emp_map.get(leader_id)
            if not leader:
                continue
            leader_slack_id = try_get_or_cache_employee_slack_user_id(db, leader)
            if not leader_slack_id:
                continue
            try:
                notify_project_leader_allocation_removed(
                    leader_slack_user_id=leader_slack_id,
                    leader_name=leader.name,
                    employee_name=removed_emp.name if removed_emp else "Employee",
                    employee_designation=removed_emp.designation if removed_emp else None,
                    sub_project_name=project.name,
                    remover_name=remover_display,
                    remover_role=actor_role_label,
                    allocated_hours_per_day=f"{alloc_info.get('total_daily_hours', 8)}h/day",
                    role_tags=alloc_info.get("role_tags", []),
                )
                logger.info("Sent Slack removal notification to leader %s for project %s (Actor: %s)", leader.name, project.name, actor_type)
            except Exception as e:
                logger.warning("Failed to notify leader %s of removal: %s", leader_id, e)

    except Exception as exc:
        logger.warning("Error in send_unallocation_notifications: %s", exc)


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
    base = (os.getenv("PORTAL_URL") or "https://portal.autonexai360.com").strip().rstrip("/")
    if base.endswith("/employee/dashboard"):
        return base
    return f"{base}/employee/dashboard"


_today_slack_reminders = {}


def record_today_slack_reminder(employee_id: int, channel_id: str, ts: str):
    if employee_id and channel_id and ts:
        _today_slack_reminders[employee_id] = (channel_id, ts)


def pop_today_slack_reminder(employee_id: int) -> tuple[str | None, str | None]:
    return _today_slack_reminders.pop(employee_id, (None, None))


def find_today_slack_reminder(user_id: str) -> tuple[str | None, str | None]:
    """Find the most recent check-in reminder/warning DM message sent to this user today from DM history."""
    try:
        channel_id = open_direct_message_channel(user_id)
        response = _slack_request("/conversations.history", {"channel": channel_id, "limit": 10}, method="GET")
        if response.get("ok"):
            messages = response.get("messages", [])
            for msg in messages:
                blocks = msg.get("blocks", [])
                has_checkin_action = any(
                    elem.get("action_id") == "checkin_now"
                    for b in blocks if b.get("type") == "actions"
                    for elem in b.get("elements", [])
                )
                text = msg.get("text", "")
                if has_checkin_action or "don't forget to check in" in text.lower() or "haven't checked in yet" in text.lower():
                    return (channel_id, msg.get("ts"))
    except Exception as exc:
        logger.warning("Could not find Slack reminder in history for user %s: %s", user_id, exc)
    return (None, None)


def send_checkin_reminder_message(*, employee_slack_user_id: str, employee_name: str) -> tuple[str | None, str | None]:
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
                            "action_id": "checkin_now",
                            "value": json.dumps({"action": "checkin_reminder"}),
                        }
                    ],
                },
            ],
        },
    )
    if response.get("ok"):
        return (response.get("ts"), channel_id)
    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


async def send_checkin_reminder_message_async(*, employee_slack_user_id: str, employee_name: str) -> bool:
    channel_id = open_direct_message_channel(employee_slack_user_id)
    portal_url = os.getenv("PORTAL_URL", "https://portal.autonexai360.com")

    response = await _async_slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": "Daily Check-in Reminder",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"Hi {employee_name}, please don't forget to <{portal_url}|check in for today>.",
                    },
                }
            ],
        },
    )
    if response.get("ok"):
        return True
    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def try_send_checkin_reminder_message(**kwargs) -> tuple[str | None, str | None]:
    try:
        return send_checkin_reminder_message(**kwargs)
    except SlackRateLimitError:
        raise
    except Exception as exc:
        logger.warning("Slack check-in reminder skipped for %s: %s", kwargs.get("employee_name"), exc)
        return False


async def try_send_checkin_reminder_message_async(**kwargs) -> bool:
    try:
        return await send_checkin_reminder_message_async(**kwargs)
    except SlackRateLimitError:
        raise
    except Exception as exc:
        logger.warning("Slack check-in reminder skipped for %s: %s", kwargs.get("employee_name"), exc)
        return (None, None)


def send_late_warning_message(*, employee_slack_user_id: str, employee_name: str) -> tuple[str | None, str | None]:
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
                            "action_id": "checkin_now",
                            "value": json.dumps({"action": "checkin_reminder"}),
                        }
                    ],
                },
            ],
        },
    )
    if response.get("ok"):
        return (response.get("ts"), channel_id)
    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def try_send_late_warning_message(**kwargs) -> tuple[str | None, str | None]:
    try:
        return send_late_warning_message(**kwargs)
    except SlackRateLimitError:
        raise
    except Exception as exc:
        logger.warning("Slack late check-in warning skipped for %s: %s", kwargs.get("employee_name"), exc)
        return (None, None)


def send_checkin_success_message(*, employee_slack_user_id: str, employee_name: str, work_mode: str, checked_in_at_str: str) -> bool:
    """Send Slack DM via PM Bot confirming successful check-in for today."""
    channel_id = open_direct_message_channel(employee_slack_user_id)
    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"You have successfully checked in for today.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":white_check_mark: *Check-in Successful!*\nHi {employee_name}, you have successfully checked in for today.",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Work Mode*\n{work_mode.upper() if work_mode else 'N/A'}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Check-in Time*\n{checked_in_at_str}",
                        },
                    ],
                },
            ],
        },
    )
    if response.get("ok"):
        return True
    logger.error(f"Failed to send Slack check-in success message: {response.get('error')}")
    return False


def try_send_checkin_success_message(**kwargs) -> bool:
    try:
        return send_checkin_success_message(**kwargs)
    except Exception as exc:
        logger.warning("Slack check-in success notification skipped: %s", exc)
        return False


def update_checkin_reminder_to_completed(*, channel_id: str, ts: str, employee_name: str | None = None) -> bool:
    """Update check-in reminder message in Slack, removing the action button and replacing it with 'You have already checked in for today'."""
    try:
        name_text = f", {employee_name}" if employee_name else ""
        response = _slack_request(
            "/chat.update",
            {
                "channel": channel_id,
                "ts": ts,
                "text": "You have already checked in for today.",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Good morning{name_text}!*",
                        },
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "✅ *You have already checked in for today.*",
                        },
                    },
                ],
            },
        )
        if response.get("ok"):
            return True
        logger.error(f"Failed to update Slack check-in reminder message: {response.get('error')}")
        return False
    except Exception as exc:
        logger.warning("Slack update check-in reminder skipped: %s", exc)
        return False


def send_admin_late_list(*, channel_id: str, stats_payload: dict) -> bool:
    """Send the late check-in report to a specific channel (e.g. admins) using the stats engine payload."""
    overall = stats_payload.get("overall", {})
    projects = stats_payload.get("projects", [])
    late_checkins = stats_payload.get("late_list", [])
    pending_checkins = stats_payload.get("pending_list", [])
    
    # 1. Overall Summary Block
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "📊 Daily Check-in & Summary Report",
                "emoji": True
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*1. Overall Check-in Summary*\n"
                        f"• 👥 *Total Active Employees:* {overall.get('total_active', 0)} (excludes employees on leave)\n"
                        f"• ✅ *Total Checked-in:* {overall.get('total_checked_in', 0)}\n"
                        f"• ⏳ *Total Pending:* {overall.get('total_pending', 0)}\n"
                        f"• ⏱️ *Average Check-in Time:* WFO: {overall.get('avg_time_wfo', '—')} | WFH: {overall.get('avg_time_wfh', '—')}\n"
                        f"• 🥇 *First Check-in:* WFO: {overall.get('first_checkin_wfo', 'None')} | WFH: {overall.get('first_checkin_wfh', 'None')}"
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Check-in Time Distribution:*\n"
                        f"  ◽ *Before 9:00 AM:* {overall.get('distribution', {}).get('before_9', 0)} employees\n"
                        f"  ◽ *9:00 AM - 10:00 AM:* {overall.get('distribution', {}).get('9_to_10', 0)} employees\n"
                        f"  ◽ *10:00 AM - 11:00 AM:* {overall.get('distribution', {}).get('10_to_11', 0)} employees\n"
                        f"  ◽ *11:00 AM - 12:00 PM:* {overall.get('distribution', {}).get('11_to_12', 0)} employees\n"
                        f"  🟥 *After 12:00 PM:* {overall.get('distribution', {}).get('after_12', 0)} employees"
            }
        },
        {"type": "divider"}
    ]

    def make_cell(text):
        return {"type": "raw_text", "text": str(text)}

    # 2. Project-Wise Summary
    if projects:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*2. Project-Wise Summary*"
            }
        })
        
        chunk_size = 50
        for i in range(0, len(projects), chunk_size):
            chunk = projects[i:i + chunk_size]
            rows = [[make_cell("Project Name"), make_cell("Check-ins"), make_cell("Pending"), make_cell("Distribution (<9 | 9-10 | 10-11 | 11-12 | >12)")]]
            for p in chunk:
                name_str = p['name'][:30] + "..." if len(p['name']) > 33 else p['name']
                chk_str = f"{p['checked_in']}/{p['allocated_total']}"
                pend_str = str(p['pending'])
                dist_str = f"{p['dist_before_9']} | {p['dist_9_to_10']} | {p['dist_10_to_11']} | {p['dist_11_to_12']} | {p['dist_after_12']}"
                rows.append([make_cell(name_str), make_cell(chk_str), make_cell(pend_str), make_cell(dist_str)])
                
            blocks.append({
                "type": "table",
                "rows": rows,
                "column_settings": [
                    {"align": "left", "is_wrapped": True},
                    {"align": "center"},
                    {"align": "center"},
                    {"align": "center", "is_wrapped": True}
                ]
            })
            
        blocks.append({"type": "divider"})

    def add_table_section(title, employee_list, list_type="pending"):
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
            if list_type == "late":
                rows = [[make_cell("S.No"), make_cell("Employee Name"), make_cell("Email"), make_cell("Time")]]
                for idx, record in enumerate(chunk):
                    name, email, time_str = record
                    rows.append([make_cell(i + idx + 1), make_cell(name), make_cell(email), make_cell(time_str)])
                col_settings = [{"align": "center"}, {"align": "left", "is_wrapped": True}, {"align": "left", "is_wrapped": True}, {"align": "right"}]
            elif list_type == "sentiment":
                rows = [[make_cell("S.No"), make_cell("Employee Name"), make_cell("Email"), make_cell("Sentiment")]]
                for idx, record in enumerate(chunk):
                    name, email, mood_str = record
                    rows.append([make_cell(i + idx + 1), make_cell(name), make_cell(email), make_cell(mood_str)])
                col_settings = [{"align": "center"}, {"align": "left", "is_wrapped": True}, {"align": "left", "is_wrapped": True}, {"align": "left"}]
            else:
                rows = [[make_cell("S.No"), make_cell("Employee Name"), make_cell("Email")]]
                for idx, record in enumerate(chunk):
                    name, email = record
                    rows.append([make_cell(i + idx + 1), make_cell(name), make_cell(email)])
                col_settings = [{"align": "center"}, {"align": "left", "is_wrapped": True}, {"align": "left", "is_wrapped": True}]
                
            blocks.append({
                "type": "table",
                "rows": rows,
                "column_settings": col_settings
            })

    add_table_section("Checked In Late (After 11:00 AM)", late_checkins, list_type="late")
    add_table_section("Pending (Not Checked In Yet)", pending_checkins, list_type="pending")
    add_table_section("⚠️ Low Sentiment Check-ins", stats_payload.get("low_sentiment_list", []), list_type="sentiment")

    max_blocks = 40
    main_message_ts = None
    text = f"Daily Late Check-in Report (Late: {len(late_checkins)} | Pending: {len(pending_checkins)})"
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def send_chunk(payload):
        resp = _slack_request("/chat.postMessage", payload)
        if not resp.get("ok"):
            raise RuntimeError(f"Slack message chunk failed: {resp.get('error') or 'unknown_error'}")
        return resp
    
    for i in range(0, len(blocks), max_blocks):
        chunk = blocks[i:i + max_blocks]
        payload = {"channel": channel_id, "text": text if not main_message_ts else "Report Continued...", "blocks": chunk}
        
        if main_message_ts:
            payload["thread_ts"] = main_message_ts
            
        response = send_chunk(payload)
            
        if not main_message_ts:
            main_message_ts = response.get("ts")
            
    return True


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
    except SlackRateLimitError:
        raise
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
