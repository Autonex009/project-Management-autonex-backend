"""Standalone script to generate and dispatch the Daily Admin Late Check-in Report to Slack.

Usage:
    .venv/Scripts/python.exe scripts/run_admin_report.py
    .venv/Scripts/python.exe scripts/run_admin_report.py --dry-run
    .venv/Scripts/python.exe scripts/run_admin_report.py --channel C0BV91K4PD5
"""
import argparse
import logging
import os
import sys
from datetime import datetime

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

# Setup logging to stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("run_admin_report")

# Preload all SQLAlchemy models so relationships and mappers resolve
from app.models import (  # noqa: F401
    project, allocation, leave, employee, parent_project, user, sub_project,
    guideline, side_project, skill, notification, wfh, signup_request, referral,
    payroll, performance_review, perf_eval, onboarding, company_settings,
    wifi_network, chat, encord_analytics, encord_activity, vendor,
    audit_log, employee_badge, onboarding_pipeline, daily_checkin
)

from app.db.database import SessionLocal
from app.utils.business_time import today_ist
from app.constants.leave_types import is_fixed_holiday, is_weekend
from app.services.stats_engine import _generate_admin_report_payload
from app.services.slack_service import send_admin_late_list

DEFAULT_CHANNEL = os.getenv("LATE_CHECKIN_CHANNEL_ID", "C0BV91K4PD5")


def main():
    parser = argparse.ArgumentParser(description="Run the daily Admin Check-in Report")
    parser.add_argument("--dry-run", action="store_true", help="Generate stats payload and display summary without sending Slack message")
    parser.add_argument("--date", type=str, default=None, help="Target date in YYYY-MM-DD format (defaults to today IST)")
    parser.add_argument("--channel", type=str, default=DEFAULT_CHANNEL, help="Slack Channel ID to post report to")
    parser.add_argument("--force", action="store_true", help="Force send even if today is a weekend or holiday")
    args = parser.parse_args()

    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target_date = today_ist()

    logger.info("Running Admin Report for target date: %s", target_date)
    logger.info("Destination Slack Channel: %s", args.channel)

    if not args.force and (is_weekend(target_date) or is_fixed_holiday(target_date)):
        logger.warning("Target date %s is a weekend or holiday. Use --force to run anyway.", target_date)
        sys.exit(0)

    db = SessionLocal()
    try:
        logger.info("Querying database to generate stats engine payload...")
        stats_payload = _generate_admin_report_payload(db, target_date=target_date)

        overall = stats_payload.get("overall", {})
        projects = stats_payload.get("projects", [])
        late_list = stats_payload.get("late_list", [])
        pending_list = stats_payload.get("pending_list", [])
        low_sentiment = stats_payload.get("low_sentiment_list", [])

        logger.info("=== SUMMARY STATS ===")
        logger.info("Total Active Employees (excl. leave): %s", overall.get("total_active"))
        logger.info("Total Checked In: %s", overall.get("total_checked_in"))
        logger.info("Total Pending Check-in: %s", overall.get("total_pending"))
        logger.info("Average Time - WFO: %s | WFH: %s", overall.get("avg_time_wfo"), overall.get("avg_time_wfh"))
        logger.info("First Check-in - WFO: %s", overall.get("first_checkin_wfo"))
        logger.info("First Check-in - WFH: %s", overall.get("first_checkin_wfh"))
        logger.info("Active Projects with Allocations: %s", len(projects))
        logger.info("Late Check-ins count: %s", len(late_list))
        logger.info("Pending Employees count: %s", len(pending_list))
        logger.info("Low Sentiment Check-ins count: %s", len(low_sentiment))

        if args.dry_run:
            logger.info("[DRY RUN] Skipping Slack dispatch.")
            return

        if not late_list and not pending_list:
            logger.info("No late or pending check-ins today! Report will be skipped.")
            return

        logger.info("Dispatching Admin Report to Slack channel %s...", args.channel)
        success = send_admin_late_list(channel_id=args.channel, stats_payload=stats_payload)
        if success:
            logger.info(" Admin report successfully sent to Slack channel %s!", args.channel)
        else:
            logger.error("❌ send_admin_late_list returned False.")
            sys.exit(1)

    except Exception as exc:
        logger.exception("❌ Error running admin report: %s", exc)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
