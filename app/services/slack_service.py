import json
import logging
import os
import asyncio
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
) -> bool:
    channel_id = open_direct_message_channel(pm_slack_user_id)
    project_lines = impacted_projects or ["No active project mapping found"]
    projects_text = "\n".join(f"• {line}" for line in project_lines)
    normalized_reason = reason.strip() if isinstance(reason, str) and reason.strip() else "No reason provided"

    response = _slack_request(
        "/chat.postMessage",
        {
            "channel": channel_id,
            "text": f"New leave request from {employee_name} ({start_date} to {end_date})",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*New leave request received*\n{employee_name} has submitted a leave request in Autonex.",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*PM*\n{pm_name}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Employee*\n{employee_name}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Email*\n{employee_email}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Designation*\n{employee_designation or 'N/A'}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Leave Type*\n{leave_type}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*Duration*\n{duration_days} day(s)",
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
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Reason*\n{normalized_reason}",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Impacted Projects*\n{projects_text}",
                    },
                },
            ],
        },
    )

    if response.get("ok"):
        return True

    raise RuntimeError(f"Slack message failed: {response.get('error') or 'unknown_error'}")


def try_send_pm_leave_request_message(**kwargs) -> bool:
    try:
        return send_pm_leave_request_message(**kwargs)
    except Exception as exc:
        logger.warning("Slack PM leave request notification skipped: %s", exc)
        return False


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
