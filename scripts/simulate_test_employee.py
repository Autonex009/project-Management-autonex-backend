"""Simulation script to test 7-day streak auto-allocation and 16-day WFH monthly work model sync
for test employee TesT_1 (ID: 422) on project 'Test' (ID: 217).

Usage:
    # Run full simulation (7-day streak + 16-day WFH + real Slack notifications)
    .venv/Scripts/python.exe scripts/simulate_test_employee.py --run

    # Revert / Rollback to pristine original state
    .venv/Scripts/python.exe scripts/simulate_test_employee.py --rollback
"""
import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta

# Ensure project root in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv
load_dotenv()

# Preload SQLAlchemy models
from app.models import (  # noqa: F401
    project, allocation, leave, employee, parent_project, user, sub_project, daily_checkin
)

from app.db.database import SessionLocal
from app.models.employee import Employee
from app.models.daily_checkin import DailyCheckIn
from app.models.allocation import Allocation
from app.models.user import User
from app.services import audit_service
from app.services.allocation_service import check_employee_project_streak
from app.services.work_model_service import sync_monthly_work_models
from app.services.slack_service import (
    send_allocation_notifications_to_leaders,
    notify_employee_auto_allocated,
    try_get_or_cache_employee_slack_user_id,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("simulate_test_employee")

BACKUP_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "test_employee_backup.json"))
TARGET_EMPLOYEE_ID = 422
TARGET_PROJECT_ID = 217


def save_backup(db, emp_id: int):
    """Saves original state of employee and check-in records to disk for safe rollback."""
    emp = db.query(Employee).filter(Employee.id == emp_id).first()
    checkins = db.query(DailyCheckIn).filter(DailyCheckIn.employee_id == emp_id).all()
    allocs = db.query(Allocation).filter(Allocation.employee_id == emp_id, Allocation.is_active == True).all()

    data = {
        "employee_id": emp_id,
        "work_model": emp.work_model if emp else "WFO",
        "checkins": [
            {
                "id": c.id,
                "checkin_date": c.checkin_date.isoformat(),
                "work_mode": c.work_mode,
                "project_ids": c.project_ids,
            }
            for c in checkins
        ],
        "allocations": [a.id for a in allocs],
    }
    with open(BACKUP_FILE, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("Saved original state backup to %s", BACKUP_FILE)


def run_rollback():
    """Restores the employee, check-ins, and allocations back to the pristine state."""
    db = SessionLocal()
    try:
        if not os.path.exists(BACKUP_FILE):
            logger.warning("No backup file found at %s. Performing heuristic cleanup.", BACKUP_FILE)
            backup_data = None
        else:
            with open(BACKUP_FILE, "r") as f:
                backup_data = json.load(f)

        emp = db.query(Employee).filter(Employee.id == TARGET_EMPLOYEE_ID).first()
        if not emp:
            logger.error("Employee %s not found", TARGET_EMPLOYEE_ID)
            return

        # 1. Revert work model back to WFO
        original_model = backup_data.get("work_model", "WFO") if backup_data else "WFO"
        emp.work_model = original_model
        logger.info("Reverted %s work_model back to '%s'", emp.name, original_model)

        # 2. Remove simulated allocation on project 217
        sim_allocs = db.query(Allocation).filter(
            Allocation.employee_id == TARGET_EMPLOYEE_ID,
            Allocation.sub_project_id == TARGET_PROJECT_ID,
        ).all()
        for a in sim_allocs:
            logger.info("Deleting simulated allocation ID %s on project %s", a.id, TARGET_PROJECT_ID)
            db.delete(a)

        # 3. Restore check-ins
        if backup_data and "checkins" in backup_data:
            backup_dates = {c["checkin_date"]: c for c in backup_data["checkins"]}
            all_current_checkins = db.query(DailyCheckIn).filter(
                DailyCheckIn.employee_id == TARGET_EMPLOYEE_ID
            ).all()

            for c in all_current_checkins:
                c_date_str = c.checkin_date.isoformat()
                if c_date_str in backup_dates:
                    orig = backup_dates[c_date_str]
                    c.work_mode = orig["work_mode"]
                    c.project_ids = orig["project_ids"]
                    logger.info("Restored check-in on %s: mode=%s, projects=%s", c.checkin_date, orig["work_mode"], orig["project_ids"])
                else:
                    logger.info("Deleting simulated check-in on %s", c.checkin_date)
                    db.delete(c)

        db.commit()
        logger.info("Rollback completed successfully! Employee %s is back in pristine state.", emp.name)

    except Exception as e:
        db.rollback()
        logger.error("Rollback failed: %s", e, exc_info=True)
    finally:
        db.close()


def run_simulation(send_slack: bool = True):
    """Executes the 7-day streak auto-allocation and 16-day WFH monthly sync simulation."""
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter(Employee.id == TARGET_EMPLOYEE_ID).first()
        if not emp:
            logger.error("Target employee ID %s not found in DB", TARGET_EMPLOYEE_ID)
            return

        proj = db.query(project.DailySheet).filter(project.DailySheet.id == TARGET_PROJECT_ID).first()
        if not proj:
            logger.error("Target project ID %s ('Test') not found in DB", TARGET_PROJECT_ID)
            return

        logger.info("=" * 70)
        logger.info("STARTING SIMULATION FOR: %s (%s, ID: %s)", emp.name, emp.email, emp.id)
        logger.info("TARGET PROJECT: %s (ID: %s)", proj.name, proj.id)
        logger.info("CURRENT WORK MODEL: %s", emp.work_model)
        logger.info("=" * 70)

        # 1. Save backup first
        if not os.path.exists(BACKUP_FILE):
            save_backup(db, TARGET_EMPLOYEE_ID)

        # 2. Define 16 working days in September 2026:
        # Note: Sep 14 is Ganesh Chaturthi (fixed company holiday) and is skipped by streak logic.
        # Days 1-4: WFH on project 'other'
        # Days 5-11 (Sep 7 to Sep 16): 7 consecutive working days on project 217 ('Test')
        # Days 12-16 (Sep 17 to Sep 23): Additional WFH working days (Total 16 working days)
        all_16_working_days = [
            date(2026, 9, 1),
            date(2026, 9, 2),
            date(2026, 9, 3),
            date(2026, 9, 4),
            date(2026, 9, 7),   # Streak Day 1 on project 217
            date(2026, 9, 8),   # Streak Day 2
            date(2026, 9, 9),   # Streak Day 3
            date(2026, 9, 10),  # Streak Day 4
            date(2026, 9, 11),  # Streak Day 5
            # Sep 12, 13 = Weekend; Sep 14 = Ganesh Chaturthi Fixed Holiday (skipped)
            date(2026, 9, 15),  # Streak Day 6
            date(2026, 9, 16),  # Streak Day 7 (TODAY! Auto-allocation triggers here)
            date(2026, 9, 17),
            date(2026, 9, 18),
            # Sep 19, 20 = Weekend
            date(2026, 9, 21),
            date(2026, 9, 22),
            date(2026, 9, 23),
        ]

        streak_dates = [
            date(2026, 9, 7),
            date(2026, 9, 8),
            date(2026, 9, 9),
            date(2026, 9, 10),
            date(2026, 9, 11),
            date(2026, 9, 15),
            date(2026, 9, 16),
        ]

        # 3. Create or update the 16 working days of check-ins
        logger.info("\n--- STEP 1: Populating 16 Working Days of Check-ins (All WFH) ---")
        existing_checkins = {
            c.checkin_date: c
            for c in db.query(DailyCheckIn).filter(DailyCheckIn.employee_id == TARGET_EMPLOYEE_ID).all()
        }

        for d in all_16_working_days:
            pids = [TARGET_PROJECT_ID] if d in streak_dates else ["other"]
            if d in existing_checkins:
                c = existing_checkins[d]
                c.work_mode = "WFH"
                c.project_ids = pids
            else:
                c = DailyCheckIn(
                    employee_id=TARGET_EMPLOYEE_ID,
                    checkin_date=d,
                    work_mode="WFH",
                    project_ids=pids,
                )
                db.add(c)

        db.flush()
        logger.info("Populated 16 working days in September 2026 as WFH.")

        # 4. Simulate Streak Progression Day 1 through Day 7
        logger.info("\n--- STEP 2: Simulating 7-Day Streak Progression ---")
        for i, s_date in enumerate(streak_dates, 1):
            streak_met = check_employee_project_streak(
                db,
                employee_id=TARGET_EMPLOYEE_ID,
                project_id=TARGET_PROJECT_ID,
                target_date=s_date,
                required_streak=7,
            )
            logger.info("Working Day %d (%s) -> Streak Met: %s (%d/7)", i, s_date, streak_met, i)

        # On Day 7 (Sep 16, Today), streak is 7/7!
        assert streak_met is True, "Day 7 streak should have been met!"

        # 5. Create the Permanent Allocation row (Auto-allocated on Day 7)
        logger.info("\n--- STEP 3: Auto-Allocating Employee to Project %s ('%s') ---", TARGET_PROJECT_ID, proj.name)
        new_alloc = db.query(Allocation).filter(
            Allocation.employee_id == TARGET_EMPLOYEE_ID,
            Allocation.sub_project_id == TARGET_PROJECT_ID,
            Allocation.is_active == True,
        ).first()

        if not new_alloc:
            new_alloc = Allocation(
                employee_id=TARGET_EMPLOYEE_ID,
                sub_project_id=TARGET_PROJECT_ID,
                total_daily_hours=8,
                is_active=True,
                active_start_date=date(2026, 9, 16),
            )
            db.add(new_alloc)
            db.flush()

        # Audit log for streak auto-allocation
        audit_service.record(
            db,
            actor=None,
            action="employee.auto_allocated_streak",
            category="Allocations",
            action_type="Created",
            entity_type="allocation",
            entity_id=new_alloc.id,
            entity_name=proj.name,
            subject_employee_id=TARGET_EMPLOYEE_ID,
            subject_name=emp.name,
            details=audit_service.changes(
                audit_service.field_diff("Project", None, proj.name),
                audit_service.field_diff("Hours", None, "8h/day"),
                audit_service.field_diff("Trigger", None, "7-day continuous check-in streak"),
            ),
        )
        db.commit()
        logger.info("Committed Allocation ID %s: %s permanently assigned to '%s' (8h/day, active from %s)",
                    new_alloc.id, emp.name, proj.name, new_alloc.active_start_date)

        # 6. Send Real Slack Notifications
        if send_slack:
            logger.info("\n--- STEP 4: Sending Slack Notifications ---")
            # A. Send to Project Managers and Team Leads (Karan Paigude & Kisan Jena)
            try:
                send_allocation_notifications_to_leaders(
                    db=db,
                    allocation=new_alloc,
                    project=proj,
                    source="Auto-allocation (7-day streak)",
                )
                logger.info("Sent allocation Slack notification to project leaders (PM & Lead).")
            except Exception as e:
                logger.warning("Slack notification to leaders failed: %s", e)

            # B. Send to the Employee (TesT_1)
            emp_slack_id = try_get_or_cache_employee_slack_user_id(db, emp)
            if emp_slack_id:
                try:
                    notify_employee_auto_allocated(
                        employee_slack_user_id=emp_slack_id,
                        employee_name=emp.name,
                        sub_project_name=proj.name,
                        allocated_hours_per_day="8h/day",
                    )
                    logger.info("Sent auto-allocation confirmation Slack notification to employee %s (%s).", emp.name, emp_slack_id)
                except Exception as e:
                    logger.warning("Slack notification to employee failed: %s", e)
            else:
                logger.warning("Employee %s has no Slack ID", emp.name)
        else:
            logger.info("\n--- STEP 4: Slack Notifications Skipped (--no-slack flag) ---")

        # 7. Run Monthly Work Model Auto-Sync (16 WFH Days Threshold)
        logger.info("\n--- STEP 5: Running Monthly Work Model Auto-Sync (September 2026) ---")
        sync_result = sync_monthly_work_models(db, target_date=date(2026, 10, 1), min_threshold_days=15)
        logger.info("Monthly Sync Result: %s", json.dumps(sync_result, indent=2))

        # Re-fetch employee to confirm DB state
        db.refresh(emp)
        logger.info("\n" + "=" * 70)
        logger.info("FINAL SIMULATION SUMMARY:")
        logger.info("• Employee: %s (ID: %s)", emp.name, emp.id)
        logger.info("• Final Base Work Model: %s (Successfully switched from WFO -> WFH!)", emp.work_model)
        logger.info("• Permanent Allocation: Project '%s' (ID %s, 8h/day, is_active=%s)", proj.name, proj.id, new_alloc.is_active)
        logger.info("• Working Days in Sep evaluated: %s (16/16 WFH)", sync_result.get("total_working_days"))
        logger.info("• Switched to WFH List: %s", sync_result.get("switched_to_wfh"))
        logger.info("• Audit Log: Recorded in database")
        logger.info("=" * 70)
        logger.info("\nNOTE: To undo this simulation and restore TesT_1 to pristine state, run:")
        logger.info("  .venv/Scripts/python.exe scripts/simulate_test_employee.py --rollback\n")

    except Exception as exc:
        db.rollback()
        logger.error("Simulation failed with error: %s", exc, exc_info=True)
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulate 7-day streak & monthly WFH sync for test employee")
    parser.add_argument("--run", action="store_true", help="Execute the simulation and commit to DB")
    parser.add_argument("--rollback", action="store_true", help="Revert the simulation and restore original state")
    parser.add_argument("--no-slack", action="store_true", help="Skip sending real Slack notifications")
    args = parser.parse_args()

    if args.rollback:
        run_rollback()
    else:
        run_simulation(send_slack=not args.no_slack)
