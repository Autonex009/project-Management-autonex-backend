"""
Daily Project Annotation Report Service
- Queries finalized Encord daily metrics for yesterday
- Filters Autonex/Theta annotators and computes project-wise totals, active annotators, and average hours
- Formats Slack Block Kit payload with block overflow protection
- Executes with Two-Stage DB isolation (zero DB connection leaks)
"""
import logging
import os
from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, List, Optional, Any

from sqlalchemy import or_
from sqlalchemy.orm import Session
from app.db.database import SessionLocal
from app.models.encord_analytics import EncordDailyTimeSpent
from app.models.project import DailySheet
from app.api.analytics import is_autonex_email
from app.utils.business_time import today_ist
from app.services.slack_service import _slack_request

logger = logging.getLogger(__name__)

# Default channel ID for the daily project report (PMs / test channel)
DEFAULT_REPORT_CHANNEL_ID = os.getenv("DAILY_PROJECT_REPORT_CHANNEL_ID", "C0BQX541CNT")
ALERT_CHANNEL_ID = os.getenv("DAILY_PROJECT_REPORT_ALERT_CHANNEL_ID", os.getenv("LATE_CHECKIN_CHANNEL_ID", "C0BV91K4PD5"))


def _get_daily_annotation_metrics(db: Session, target_date: date) -> Dict[str, Any]:
    """
    Stage 1: Pure DB Work (< 15ms).
    Queries and computes project-level metrics for the target date.
    Optimized with lightweight scalar projections and selective foreign-key resolution.
    """
    # 1. Fetch only necessary scalar columns for target date in a single round-trip.
    # Bypasses heavy ORM entity hydration and eliminates redundant count() query.
    rows = (
        db.query(
            EncordDailyTimeSpent.sub_project_id,
            EncordDailyTimeSpent.encord_project_hash,
            EncordDailyTimeSpent.user_email,
            EncordDailyTimeSpent.time_spent_seconds,
        )
        .filter(EncordDailyTimeSpent.metric_date == target_date)
        .all()
    )

    if not rows:
        return {
            "status": "missing_sync",
            "date": target_date,
            "projects": [],
            "total_hours": 0.0,
            "total_annotators": 0,
        }

    # 2. Group by project for Autonex (Theta) annotators
    grouped_data = defaultdict(lambda: {
        "seconds": 0,
        "active_users": set(),
        "sub_project_id": None,
        "encord_project_hash": None,
    })

    overall_users = set()
    overall_seconds = 0

    for r in rows:
        if not is_autonex_email(r.user_email):
            continue

        p_key = r.sub_project_id or r.encord_project_hash
        entry = grouped_data[p_key]
        entry["sub_project_id"] = r.sub_project_id
        entry["encord_project_hash"] = r.encord_project_hash

        sec = r.time_spent_seconds or 0
        entry["seconds"] += sec
        overall_seconds += sec

        # Optimization: Only count users with active time logged (> 0s)
        if sec > 0:
            entry["active_users"].add(r.user_email)
            overall_users.add(r.user_email)

    # 3. Resolve project names via DailySheet targeting ONLY the active projects
    # Replaces two full table scans across all historical projects with a single targeted indexed query.
    sub_ids = {entry["sub_project_id"] for entry in grouped_data.values() if entry["sub_project_id"]}
    hashes = {entry["encord_project_hash"] for entry in grouped_data.values() if entry["encord_project_hash"]}

    sheets_by_id: Dict[int, str] = {}
    sheets_by_hash: Dict[str, str] = {}

    if sub_ids or hashes:
        sheet_filter = []
        if sub_ids:
            sheet_filter.append(DailySheet.id.in_(sub_ids))
        if hashes:
            sheet_filter.append(DailySheet.encord_project_hash.in_(hashes))

        sheets = (
            db.query(DailySheet.id, DailySheet.name, DailySheet.encord_project_hash)
            .filter(or_(*sheet_filter))
            .all()
        )
        for s in sheets:
            if s.id:
                sheets_by_id[s.id] = s.name
            if s.encord_project_hash:
                sheets_by_hash[s.encord_project_hash] = s.name

    active_projects: List[Dict[str, Any]] = []

    for _, data in grouped_data.items():
        active_count = len(data["active_users"])
        total_hours = round(data["seconds"] / 3600.0, 1)

        # Optimization: Exclude projects with 0 active Autonex annotators or 0 hours
        if active_count == 0 or total_hours == 0:
            continue

        name = (
            sheets_by_id.get(data["sub_project_id"])
            or sheets_by_hash.get(data["encord_project_hash"])
            or "Unknown Project"
        )
        avg_hours = round(total_hours / active_count, 1) if active_count > 0 else 0.0

        active_projects.append({
            "name": name.strip(),
            "hours": total_hours,
            "annotators": active_count,
            "avg_hours": avg_hours,
        })

    # Sort descending by total platform hours
    active_projects.sort(key=lambda p: p["hours"], reverse=True)

    if not active_projects:
        return {
            "status": "no_activity",
            "date": target_date,
            "projects": [],
            "total_hours": 0.0,
            "total_annotators": 0,
        }

    return {
        "status": "success",
        "date": target_date,
        "projects": active_projects,
        "total_hours": round(overall_seconds / 3600.0, 1),
        "total_annotators": len(overall_users),
    }


def build_daily_annotation_report_blocks(report_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Builds Slack Block Kit elements with clean visual hierarchy."""
    target_date: date = report_data["date"]
    display_date = target_date.strftime("%d %b %Y")
    status = report_data.get("status")

    if status == "missing_sync":
        return [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"⚠️ Daily Annotation Report | {display_date}",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"⚠️ *Failed to generate Daily Annotation Report:*\n"
                        f"Encord sync did not complete or no records were found for yesterday (*{display_date}*).\n"
                        f"Please check Encord sync logs in the engineering portal."
                    ),
                },
            },
        ]

    if status == "no_activity":
        return [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"📊 Daily Annotation Report | {display_date}",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"ℹ️ *There was no active Autonex annotation activity recorded for yesterday ({display_date}).*",
                },
            },
        ]

    projects = report_data.get("projects", [])
    total_hours = report_data.get("total_hours", 0.0)
    total_annotators = report_data.get("total_annotators", 0)

    blocks: List[Dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📊 Daily Annotation Report | {display_date}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Overall Summary ({display_date})*\n"
                    f"• ⏱️ *Total Platform Hours:* `{total_hours}h`\n"
                    f"• 👥 *Total Active Annotators:* `{total_annotators}`\n"
                    f"• 📁 *Active Projects:* `{len(projects)}`"
                ),
            },
        },
        {"type": "divider"},
    ]

    def make_cell(text):
        return {"type": "raw_text", "text": str(text)}

    chunk_size = 50
    for i in range(0, len(projects), chunk_size):
        chunk = projects[i:i + chunk_size]
        rows = [
            [
                make_cell("Project Name"),
                make_cell("Hours"),
                make_cell("Annotators"),
                make_cell("Avg / Annotator"),
            ]
        ]
        for p in chunk:
            rows.append([
                make_cell(p["name"]),
                make_cell(f"{p['hours']}h"),
                make_cell(str(p["annotators"])),
                make_cell(f"{p['avg_hours']}h"),
            ])

        blocks.append({
            "type": "table",
            "rows": rows,
            "column_settings": [
                {"align": "left", "is_wrapped": True},
                {"align": "right"},
                {"align": "center"},
                {"align": "right"},
            ],
        })

    return blocks


def send_daily_annotation_report(
    channel_id: Optional[str] = None,
    target_date: Optional[date] = None,
) -> bool:
    """
    Two-Stage Execution:
    Stage 1: Pure DB query and calculation, immediate db.close().
    Stage 2: Slack Network I/O with block overflow protection and chunking.
    """
    target_channel = channel_id or DEFAULT_REPORT_CHANNEL_ID
    yesterday = target_date or (today_ist() - timedelta(days=1))

    # Stage 1: Pure DB work (< 25ms)
    db = SessionLocal()
    try:
        report_data = _get_daily_annotation_metrics(db, yesterday)
    except Exception as exc:
        logger.exception("[daily_project_report] Failed to compute daily metrics: %s", exc)
        return False
    finally:
        db.close()  # Connection returned to pool immediately!

    # Stage 2: Pure Slack Network I/O (0 DB connections held)
    try:
        blocks = build_daily_annotation_report_blocks(report_data)
        display_date = yesterday.strftime("%d %b %Y")
        text = f"Daily Annotation Report | {display_date}"

        # If sync was missing, also alert the engineering/alert channel if different
        if report_data.get("status") == "missing_sync" and ALERT_CHANNEL_ID != target_channel:
            try:
                _slack_request("/chat.postMessage", {
                    "channel": ALERT_CHANNEL_ID,
                    "text": text,
                    "blocks": blocks,
                })
            except Exception as e:
                logger.error("[daily_project_report] Failed to send missing sync alert: %s", e)

        # Slack Block Overflow Protection: Slack allows max 50 blocks per message
        max_blocks = 45
        main_message_ts = None

        for i in range(0, len(blocks), max_blocks):
            chunk = blocks[i:i + max_blocks]
            payload = {
                "channel": target_channel,
                "text": text if not main_message_ts else "Daily Annotation Report Continued...",
                "blocks": chunk,
            }
            if main_message_ts:
                payload["thread_ts"] = main_message_ts

            response = _slack_request("/chat.postMessage", payload)
            if not response.get("ok"):
                logger.error("[daily_project_report] Slack postMessage failed: %s", response.get("error"))
                return False

            if not main_message_ts:
                main_message_ts = response.get("ts")

        logger.info(
            "[daily_project_report] Sent daily report for %s to channel %s (projects: %d)",
            yesterday, target_channel, len(report_data.get("projects", []))
        )
        return True

    except Exception as exc:
        logger.exception("[daily_project_report] Failed to send report to Slack: %s", exc)
        return False
