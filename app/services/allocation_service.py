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


def sync_employee_allocations_from_checkin(
    db: Session,
    employee_id: int,
    submitted_project_ids: List[int],
    background_tasks: BackgroundTasks,
    http_request
):
    """
    Syncs the employee's active allocations from check-in.
    Rule: Only auto-allocate if the employee is currently idle (0 allocations).
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
    
    # Phase 1: Exclude PMs, Leads, and Admins from auto-allocation
    if actor_user and actor_user.role in ("pm", "team_lead", "admin"):
        return
    
    affected_project_ids = set()
    now = datetime.utcnow()
    
    # 3. Add new projects (Auto-allocate)
    for pid in submitted_set:
        proj = db.query(Project).filter(Project.id == pid).first()
        
        # Phase 1: Never auto-allocate to an archived project
        if not proj or proj.project_status == "archived":
            continue
            
        new_alloc = Allocation(
            employee_id=employee_id,
            sub_project_id=pid,
            total_daily_hours=8,
            is_active=True,
            active_start_date=now.date(),
        )
        db.add(new_alloc)
        db.flush() # flush to get the ID for audit
        
        affected_project_ids.add(pid)
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
            summary=f"{emp_name} was auto-allocated to {p_name} via Daily Check-in.",
            request=http_request
        )
            
    # 4. Dispatch Background Task with safe DB session
    if affected_project_ids:
        background_tasks.add_task(_background_sync_projects, affected_project_ids)
