from sqlalchemy.orm import Session
from datetime import datetime
from typing import List, Set
from fastapi import BackgroundTasks
from app.db.database import SessionLocal
from app.models.allocation import Allocation
from app.models.employee import Employee
import app.models.sub_project  # Required to prevent SQLAlchemy mapper Initialization errors
from app.models.project import DailySheet as Project
from app.api.allocations import sync_project_allocations
from app.services import audit_service


def _background_sync_projects(project_ids: Set[int]):
    """Safely runs in background with its own DB session to prevent 'Session Closed' errors."""
    with SessionLocal() as bg_db:
        try:
            for pid in project_ids:
                sync_project_allocations(bg_db, pid)
            bg_db.commit()
        except Exception as e:
            bg_db.rollback()
            import logging
            logging.getLogger(__name__).error(f"Error in background project sync: {e}")


def _background_notify_allocation(allocation_id: int, project_id: int, source: str):
    """Safely runs in background with its own DB session to send Slack messages to PM/Lead and employee."""
    with SessionLocal() as bg_db:
        try:
            alloc = bg_db.query(Allocation).filter(Allocation.id == allocation_id).first()
            proj = bg_db.query(Project).filter(Project.id == project_id).first()
            if not alloc or not proj:
                return

            from app.services.slack_service import (
                send_allocation_notifications_to_leaders,
                try_get_or_cache_employee_slack_user_id,
                notify_employee_auto_allocated,
            )

            # 1. Notify PM(s) and Lead(s) of the project
            send_allocation_notifications_to_leaders(bg_db, alloc, proj, source=source)

            # 2. Notify the newly allocated employee
            emp = bg_db.query(Employee).filter(Employee.id == alloc.employee_id).first()
            if emp:
                emp_slack_id = try_get_or_cache_employee_slack_user_id(bg_db, emp)
                if emp_slack_id:
                    notify_employee_auto_allocated(
                        employee_slack_user_id=emp_slack_id,
                        employee_name=emp.name,
                        sub_project_name=proj.name,
                        allocated_hours_per_day=f"{alloc.total_daily_hours or 8}h/day",
                    )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Error in background allocation notification: %s", e)


def check_employee_project_streak(
    db: Session,
    employee_id: int,
    project_id: int,
    target_date,
    required_streak: int = 7
) -> bool:
    """
    Checks if an employee has checked into project_id for `required_streak` consecutive working days
    ending on target_date.
    Working days exclude weekends, fixed holidays, and approved leaves.
    Missing a working day check-in or omitting the project breaks the streak.
    """
    from datetime import timedelta
    from app.constants.leave_types import is_weekend, is_fixed_holiday
    from app.models.leave import Leave
    from app.models.daily_checkin import DailyCheckIn

    # Day 1 is today (already checked in with project_id)
    max_lookback = 30
    min_date = target_date - timedelta(days=max_lookback)

    # Bulk query 1: All approved leaves for this employee within lookback window
    leaves = db.query(Leave.start_date, Leave.end_date).filter(
        Leave.employee_id == employee_id,
        Leave.status == "approved",
        Leave.start_date <= target_date,
        Leave.end_date >= min_date,
    ).all()

    approved_leave_dates = set()
    for l_start, l_end in leaves:
        l_curr = max(l_start, min_date)
        l_limit = min(l_end, target_date)
        while l_curr <= l_limit:
            approved_leave_dates.add(l_curr)
            l_curr += timedelta(days=1)

    # Bulk query 2: All check-ins for this employee within lookback window
    checkins = db.query(DailyCheckIn.checkin_date, DailyCheckIn.project_ids).filter(
        DailyCheckIn.employee_id == employee_id,
        DailyCheckIn.checkin_date >= min_date,
        DailyCheckIn.checkin_date <= target_date,
    ).all()

    checkin_by_date = {}
    for chk in checkins:
        pids = chk.project_ids if isinstance(chk.project_ids, list) else []
        checkin_by_date[chk.checkin_date] = pids

    # If target_date is in checkin_by_date, verify it contains project_id
    if target_date in checkin_by_date:
        today_pids = checkin_by_date[target_date]
        if not any(p == project_id or str(p) == str(project_id) for p in today_pids):
            return False

    streak_count = 1
    current_date = target_date - timedelta(days=1)
    days_examined = 0

    while streak_count < required_streak and days_examined < max_lookback:
        days_examined += 1

        # Skip non-working calendar days (weekends and company holidays)
        if is_weekend(current_date) or is_fixed_holiday(current_date):
            current_date -= timedelta(days=1)
            continue

        # Check for approved leaves (pauses streak without breaking it)
        if current_date in approved_leave_dates:
            current_date -= timedelta(days=1)
            continue

        # Check DailyCheckIn on this working day from in-memory cache
        pids = checkin_by_date.get(current_date)
        if not pids:
            return False

        has_proj = any(p == project_id or str(p) == str(project_id) for p in pids)
        if not has_proj:
            return False

        streak_count += 1
        current_date -= timedelta(days=1)

    return streak_count >= required_streak


def sync_employee_allocations_from_checkin(
    db: Session,
    employee_id: int,
    submitted_project_ids: List[int],
    background_tasks: BackgroundTasks,
    http_request
):
    """
    Syncs the employee's active allocations from check-in.
    Rule: Only auto-allocate if the employee is currently idle (0 allocations)
          AND has checked into the same project for 7 consecutive working days.
    Rule: NEVER auto-unallocate.
    """
    if not submitted_project_ids:
        return
        
    submitted_set = set(submitted_project_ids)
    
    # 1. Fetch current active allocations
    active_allocations = db.query(Allocation).filter(
        Allocation.employee_id == employee_id,
        Allocation.is_active == True
    ).all()
    
    # 2. Only auto-allocate if employee is completely idle
    if len(active_allocations) > 0:
        return # Do not alter permanent roster
        
    # We have an idle employee checking in to new projects!
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    emp_name = employee.name if employee else "Unknown"
    
    # We need the User object for the audit actor
    from app.models.user import User
    actor_user = db.query(User).filter(User.employee_id == employee_id).first()
    
    # Exclude PMs, Leads, and Admins from auto-allocation
    if actor_user and actor_user.role in ("pm", "team_lead", "admin"):
        return
    
    affected_project_ids = set()
    newly_allocated_ids: List[tuple[int, int]] = []
    now = datetime.utcnow()
    today = now.date()
    
    # 3. Add new projects (Auto-allocate only if 7-day streak is met)
    for pid in submitted_set:
        proj = db.query(Project).filter(Project.id == pid).first()
        
        # Never auto-allocate to an archived project
        if not proj or proj.project_status == "archived":
            continue

        # Check if 7 consecutive working days streak on this project is satisfied
        if not check_employee_project_streak(db, employee_id=employee_id, project_id=pid, target_date=today, required_streak=7):
            continue
            
        new_alloc = Allocation(
            employee_id=employee_id,
            sub_project_id=pid,
            total_daily_hours=8,
            is_active=True,
            active_start_date=today,
        )
        db.add(new_alloc)
        db.flush() # flush to get the ID for audit
        
        affected_project_ids.add(pid)
        newly_allocated_ids.append((new_alloc.id, pid))
        p_name = proj.name if proj else f"ID {pid}"
        
        # Fire Audit Log for addition
        audit_service.record(
            db,
            actor=actor_user,
            action="allocation.auto_created",
            category="Allocations",
            action_type="Created",
            entity_type="allocation",
            entity_id=new_alloc.id,
            entity_name=emp_name,
            subject_employee_id=employee_id,
            subject_name=emp_name,
            details=audit_service.changes(
                audit_service.field_diff("Project", None, p_name),
                audit_service.field_diff("Daily hours", None, 8),
                audit_service.field_diff("Streak", None, "7 consecutive working days"),
            ),
            summary=f"{emp_name} was auto-allocated to {p_name} after a 7-day continuous check-in streak.",
            request=http_request
        )
            
    # 4. Commit and Dispatch Background Tasks with safe DB session
    if affected_project_ids:
        db.commit()
        background_tasks.add_task(_background_sync_projects, affected_project_ids)
        for alloc_id, proj_id in newly_allocated_ids:
            background_tasks.add_task(
                _background_notify_allocation,
                alloc_id,
                proj_id,
                "Auto-allocation (7-day continuous check-in streak)",
            )

