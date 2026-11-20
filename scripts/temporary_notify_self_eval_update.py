"""
Temporary script: Notify active employees on Slack about the self-evaluation update.

Usage:
  # Dry-run preview (does not send messages):
  .venv/Scripts/python scripts/temporary_notify_self_eval_update.py --dry-run

  # Actually send Slack DMs to all active employees:
  .venv/Scripts/python scripts/temporary_notify_self_eval_update.py --send

  # Test send to a single specific employee ID:
  .venv/Scripts/python scripts/temporary_notify_self_eval_update.py --send --emp-id <id>
"""
import sys
import os
import argparse
import time

# Ensure backend root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.database import SessionLocal
from app.models.employee import Employee
from app.services.slack_service import (
    open_direct_message_channel,
    _slack_request,
    lookup_user_id_by_email,
)

SLACK_MESSAGE_TEXT = (
    "📢 *Update: November Monthly Self-Evaluation*\n\n"
    "Hello,\n\n"
    "Please note that the monthly self-evaluation system is currently undergoing an update with new criteria. "
    "If you have already submitted or were about to submit, please wait and start submitting your self-evaluation "
    "*after the first half today* once the update is applied.\n\n"
    "Thank you for your cooperation!"
)

SLACK_BLOCKS = [
    {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                "📢 *Notice: November Self-Evaluation System Update*\n\n"
                "Hi there,\n\n"
                "The monthly self-evaluation system is currently being updated with new evaluation criteria. "
                "Any submissions made earlier today have been reset.\n\n"
                "👉 *Please start submitting your self-evaluation after the first half today* once the update is fully deployed.\n\n"
                "We appreciate your patience and cooperation!"
            )
        }
    }
]


def main():
    parser = argparse.ArgumentParser(description="Temporary Slack Notification for Self-Evaluation Update")
    parser.add_argument("--send", action="store_true", help="Actually send messages (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without sending Slack messages")
    parser.add_argument("--emp-id", type=int, help="Send only to a specific employee ID for testing")
    args = parser.parse_args()

    is_dry_run = not args.send or args.dry_run

    db = SessionLocal()
    try:
        query = db.query(Employee).filter(Employee.status == "active")
        if args.emp_id:
            query = query.filter(Employee.id == args.emp_id)
        active_employees = query.all()

        print("=" * 60)
        print(f"TEMPORARY SLACK NOTIFIER - MODE: {'DRY-RUN (Simulated)' if is_dry_run else 'LIVE SEND'}")
        print(f"Total active employees targeted: {len(active_employees)}")
        print("=" * 60)
        print("\nMessage to be sent:\n")
        print(SLACK_MESSAGE_TEXT)
        print("\n" + "=" * 60)

        success_count = 0
        skipped_count = 0
        failed_count = 0

        for i, emp in enumerate(active_employees, 1):
            slack_id = emp.slack_user_id
            if not slack_id and emp.email:
                if not is_dry_run:
                    slack_id = lookup_user_id_by_email(emp.email)
                    if slack_id:
                        emp.slack_user_id = slack_id
                        db.add(emp)
                        db.commit()

            if not slack_id:
                print(f"[{i}/{len(active_employees)}] ⚠️ Skipped: {emp.name} (#{emp.id}) - No Slack ID found.")
                skipped_count += 1
                continue

            if is_dry_run:
                print(f"[{i}/{len(active_employees)}] 🔍 [DRY-RUN] Would send to: {emp.name} (Slack: {slack_id})")
                success_count += 1
            else:
                try:
                    channel_id = open_direct_message_channel(slack_id)
                    resp = _slack_request(
                        "/chat.postMessage",
                        {
                            "channel": channel_id,
                            "text": SLACK_MESSAGE_TEXT,
                            "blocks": SLACK_BLOCKS,
                        },
                    )
                    if resp.get("ok"):
                        print(f"[{i}/{len(active_employees)}] ✅ Sent to: {emp.name} (Slack: {slack_id})")
                        success_count += 1
                    else:
                        print(f"[{i}/{len(active_employees)}] ❌ Error for {emp.name}: {resp.get('error')}")
                        failed_count += 1
                    time.sleep(0.3)  # Gentle rate-limiting to stay well clear of Slack tiers
                except Exception as exc:
                    print(f"[{i}/{len(active_employees)}] ❌ Exception for {emp.name}: {exc}")
                    failed_count += 1

        print("\n" + "=" * 60)
        print("SUMMARY:")
        print(f"  Targeted: {len(active_employees)}")
        print(f"  Successful: {success_count}")
        print(f"  Skipped (No Slack ID): {skipped_count}")
        print(f"  Failed: {failed_count}")
        print("=" * 60)

    finally:
        db.close()


if __name__ == "__main__":
    main()
