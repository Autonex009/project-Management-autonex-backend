from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.db.database import get_db
from app.models.allocation import Allocation
from app.models.project import SubProject, Project  # SubProject with Project alias
from app.models.employee import Employee
from app.models.parent_project import MainProject
from app.models.sub_project import SubProject as HierarchySubProject
from app.schemas.allocation import (
    AllocationCreate, 
    AllocationUpdate, 
    AllocationResponse,
    AllocationValidationRequest,
    AllocationValidationResponse,
    EmployeeAllocationStatus
)
from app.services.allocation_validator import (
    validate_time_distribution,
    check_double_booking,
    check_leave_conflict,
    get_all_employees_allocation_status
)
from app.services.slack_service import (
    notify_employee_allocation_created,
    notify_employee_allocation_removed,
    notify_employee_sub_project_updated,
    try_get_or_cache_employee_slack_user_id,
)

router = APIRouter(prefix="/api/allocations", tags=["Allocations"])


def _format_avg_time_per_task(project: Project) -> str:
    return f"{project.estimated_time_per_task} hr/task"


def _format_target_tasks_per_employee(project: Project, allocation_count: int) -> str:
    if allocation_count > 0 and project.total_tasks:
        return str(round(project.total_tasks / allocation_count, 2))
    return "0"


def _format_timeline(project: Project) -> str:
    if project.start_date and project.end_date:
        return f"{project.start_date.isoformat()} to {project.end_date.isoformat()}"
    if project.start_date:
        return f"Starts {project.start_date.isoformat()}"
    if project.end_date:
        return f"Until {project.end_date.isoformat()}"
    return "N/A"


def _get_project_manager_name(db: Session, project: Project) -> str:
    pm_name = None

    if getattr(project, "main_project_id", None):
        main_project = db.query(MainProject).filter(MainProject.id == project.main_project_id).first()
        if main_project and main_project.program_manager_id:
            pm_employee = db.query(Employee).filter(Employee.id == main_project.program_manager_id).first()
            pm_name = pm_employee.name if pm_employee else None

    if not pm_name and getattr(project, "sub_project_id", None):
        hierarchy_sub_project = db.query(HierarchySubProject).filter(HierarchySubProject.id == project.sub_project_id).first()
        if hierarchy_sub_project and hierarchy_sub_project.pm_id:
            pm_employee = db.query(Employee).filter(Employee.id == hierarchy_sub_project.pm_id).first()
            pm_name = pm_employee.name if pm_employee else None

    return pm_name or "Unassigned"


def _send_employee_allocation_notification(db: Session, allocation: Allocation, project: Project | None, allocation_count: int) -> None:
    if not project:
        return

    employee = db.query(Employee).filter(Employee.id == allocation.employee_id).first()
    if not employee:
        return

    employee_slack_user_id = try_get_or_cache_employee_slack_user_id(db, employee)
    if not employee_slack_user_id:
        return

    notify_employee_allocation_created(
        employee_slack_user_id=employee_slack_user_id,
        employee_name=employee.name,
        sub_project_name=project.name,
        project_manager_name=_get_project_manager_name(db, project),
        avg_time_per_task=_format_avg_time_per_task(project),
        target_tasks_per_employee=_format_target_tasks_per_employee(project, allocation_count),
        timeline=_format_timeline(project),
        allocated_hours_per_day=f"{allocation.total_daily_hours or 8}h/day",
        role_tags=allocation.role_tags or [],
    )


def _send_employee_allocation_removed_notification(db: Session, allocation: Allocation, project: Project | None) -> None:
    if not project:
        return

    employee = db.query(Employee).filter(Employee.id == allocation.employee_id).first()
    if not employee:
        return

    employee_slack_user_id = try_get_or_cache_employee_slack_user_id(db, employee)
    if not employee_slack_user_id:
        return

    notify_employee_allocation_removed(
        employee_slack_user_id=employee_slack_user_id,
        employee_name=employee.name,
        sub_project_name=project.name,
        project_manager_name=_get_project_manager_name(db, project),
        timeline=_format_timeline(project),
        allocated_hours_per_day=f"{allocation.total_daily_hours or 8}h/day",
        role_tags=allocation.role_tags or [],
    )


def _notify_project_team_target_change(
    db: Session,
    project: Project | None,
    old_allocation_count: int,
    new_allocation_count: int,
    excluded_employee_ids: set[int] | None = None,
) -> None:
    if not project:
        return

    excluded_employee_ids = excluded_employee_ids or set()
    if old_allocation_count == new_allocation_count:
        return

    previous_target = _format_target_tasks_per_employee(project, old_allocation_count)
    current_target = _format_target_tasks_per_employee(project, new_allocation_count)
    if previous_target == current_target:
        return

    current_allocations = db.query(Allocation).filter(Allocation.sub_project_id == project.id).all()
    if not current_allocations:
        return

    changes_summary = (
        f"Team allocation changed from {old_allocation_count} to {new_allocation_count} employee(s).\n"
        f"• Your Target (Tasks/Emp): {previous_target} -> {current_target}"
    )

    for team_allocation in current_allocations:
        if team_allocation.employee_id in excluded_employee_ids:
            continue

        employee = db.query(Employee).filter(Employee.id == team_allocation.employee_id).first()
        if not employee:
            continue

        slack_user_id = try_get_or_cache_employee_slack_user_id(db, employee)
        if not slack_user_id:
            continue

        notify_employee_sub_project_updated(
            employee_slack_user_id=slack_user_id,
            employee_name=employee.name,
            sub_project_name=project.name,
            project_manager_name=_get_project_manager_name(db, project),
            avg_time_per_task=_format_avg_time_per_task(project),
            target_tasks_per_employee=current_target,
            timeline=_format_timeline(project),
            status=project.project_status,
            changes_summary=changes_summary,
        )


def enrich_allocation_response(allocation: Allocation, db: Session) -> dict:
    """Add employee and sub-project names to allocation response."""
    employee = db.query(Employee).filter(Employee.id == allocation.employee_id).first()
    sub_project = db.query(SubProject).filter(SubProject.id == allocation.sub_project_id).first()
    
    return {
        "id": allocation.id,
        "employee_id": allocation.employee_id,
        "sub_project_id": allocation.sub_project_id,
        "project_id": allocation.sub_project_id,  # Backward compatibility alias
        "total_daily_hours": allocation.total_daily_hours or 8,
        "active_start_date": allocation.active_start_date,
        "active_end_date": allocation.active_end_date,
        "role_tags": allocation.role_tags or [],
        "time_distribution": allocation.time_distribution or {},
        "override_flag": allocation.override_flag or False,
        "override_reason": allocation.override_reason,
        "productivity_override": allocation.productivity_override or 1.0,
        "weekly_hours_allocated": allocation.weekly_hours_allocated,
        "weekly_tasks_allocated": allocation.weekly_tasks_allocated,
        "effective_week": allocation.effective_week,
        "created_at": allocation.created_at,
        "updated_at": allocation.updated_at,
        "employee_name": employee.name if employee else None,
        "project_name": sub_project.name if sub_project else None,
        "sub_project_name": sub_project.name if sub_project else None
    }


@router.post("/validate", response_model=AllocationValidationResponse)
def validate_allocation(
    data: AllocationValidationRequest,
    db: Session = Depends(get_db)
):
    """
    Validate an allocation before saving.
    Performs Sum-Zero and Double-Booking checks.
    """
    errors = []
    warnings = []
    
    # Sum-Zero validation
    time_check = validate_time_distribution(
        data.total_daily_hours,
        data.time_distribution or {}
    )
    if not time_check['is_valid'] and data.time_distribution:
        errors.append(time_check['message'])
    
    # Double-booking check
    booking_check = check_double_booking(
        db=db,
        employee_id=data.employee_id,
        new_hours=data.total_daily_hours,
        active_start=data.active_start_date,
        active_end=data.active_end_date,
        exclude_allocation_id=data.exclude_allocation_id
    )
    
    if booking_check.get('is_overbooked'):
        warnings.append(booking_check['message'])

    # Leave-overlap check: informational warning only — leave days are
    # automatically excluded from capacity calculations downstream.
    leave_check = check_leave_conflict(
        db=db,
        employee_id=data.employee_id,
        alloc_start=data.active_start_date,
        alloc_end=data.active_end_date,
    )
    if leave_check["has_conflict"]:
        warnings.append(leave_check["message"])

    return AllocationValidationResponse(
        is_valid=len(errors) == 0,
        time_distribution_valid=time_check['is_valid'],
        double_booking_check=booking_check,
        errors=errors,
        warnings=warnings
    )


@router.post("", response_model=dict)
def create_allocation(data: AllocationCreate, db: Session = Depends(get_db)):
    """Create a new allocation with validation."""
    # Validate time distribution if provided
    if data.time_distribution:
        time_check = validate_time_distribution(
            data.total_daily_hours,
            data.time_distribution
        )
        if not time_check['is_valid']:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=time_check['message']
            )
    
    # Double-booking check (warn but don't block if override_flag is set)
    booking_check = check_double_booking(
        db=db,
        employee_id=data.employee_id,
        new_hours=data.total_daily_hours,
        active_start=data.active_start_date,
        active_end=data.active_end_date
    )
    
    if booking_check.get('is_overbooked') and not data.override_flag:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": booking_check['message'],
                "requires_override": True,
                "booking_details": booking_check
            }
        )

    # Leave-overlap check: informational only — leave days are excluded from
    # capacity calculations automatically; assignment is still permitted.
    leave_check = check_leave_conflict(
        db=db,
        employee_id=data.employee_id,
        alloc_start=data.active_start_date,
        alloc_end=data.active_end_date,
    )

    project = db.query(Project).filter(Project.id == data.sub_project_id).first()
    previous_count = 0
    if project:
        previous_count = db.query(Allocation).filter(
            Allocation.sub_project_id == data.sub_project_id
        ).count()

    allocation = Allocation(**data.model_dump())
    db.add(allocation)
    db.flush()  # Flush to include the new allocation in the count query

    # Sync project allocated_employees count from actual allocation records
    actual_count = 0
    if project:
        actual_count = db.query(Allocation).filter(
            Allocation.sub_project_id == data.sub_project_id
        ).count()
        project.allocated_employees = actual_count

    db.commit()
    db.refresh(allocation)

    try:
        _send_employee_allocation_notification(db, allocation, project, actual_count)
    except Exception:
        pass

    try:
        _notify_project_team_target_change(
            db,
            project,
            old_allocation_count=previous_count,
            new_allocation_count=actual_count,
            excluded_employee_ids={allocation.employee_id},
        )
    except Exception:
        pass

    response = enrich_allocation_response(allocation, db)
    if leave_check["has_conflict"]:
        response["leave_warning"] = {
            "message": leave_check["message"],
            "excluded_leaves": leave_check["conflicting_leaves"],
        }
    return response


@router.get("", response_model=List[dict])
def get_allocations(db: Session = Depends(get_db)):
    """Get all allocations with enriched data (optimized to avoid N+1 queries)."""
    allocations = db.query(Allocation).all()
    
    if not allocations:
        return []
    
    # Pre-load all employees and projects in single queries (batch loading)
    employee_ids = list(set(a.employee_id for a in allocations))
    project_ids = list(set(a.sub_project_id for a in allocations))
    
    employees = db.query(Employee).filter(Employee.id.in_(employee_ids)).all()
    projects = db.query(SubProject).filter(SubProject.id.in_(project_ids)).all()
    
    # Create lookup dictionaries for O(1) access
    employee_map = {e.id: e for e in employees}
    project_map = {p.id: p for p in projects}
    
    # Build response without additional queries
    result = []
    for allocation in allocations:
        emp = employee_map.get(allocation.employee_id)
        proj = project_map.get(allocation.sub_project_id)
        result.append({
            "id": allocation.id,
            "employee_id": allocation.employee_id,
            "sub_project_id": allocation.sub_project_id,
            "project_id": allocation.sub_project_id,
            "total_daily_hours": allocation.total_daily_hours or 8,
            "active_start_date": allocation.active_start_date,
            "active_end_date": allocation.active_end_date,
            "role_tags": allocation.role_tags or [],
            "time_distribution": allocation.time_distribution or {},
            "override_flag": allocation.override_flag or False,
            "override_reason": allocation.override_reason,
            "productivity_override": allocation.productivity_override or 1.0,
            "weekly_hours_allocated": allocation.weekly_hours_allocated,
            "weekly_tasks_allocated": allocation.weekly_tasks_allocated,
            "effective_week": allocation.effective_week,
            "created_at": allocation.created_at,
            "updated_at": allocation.updated_at,
            "employee_name": emp.name if emp else None,
            "project_name": proj.name if proj else None,
            "sub_project_name": proj.name if proj else None
        })
    
    return result


@router.get("/employee-status", response_model=dict)
def get_employee_allocation_status(
    active_only: bool = True,
    db: Session = Depends(get_db)
):
    """
    Get allocation status for all employees, grouped by status.
    Used for UI filtering (Unallocated/Partial/Full).
    """
    return get_all_employees_allocation_status(db, active_only)


@router.get("/by-project/{project_id}", response_model=List[dict])
def get_allocations_by_project(project_id: int, db: Session = Depends(get_db)):
    """Get all allocations for a specific project."""
    allocations = db.query(Allocation).filter(
        Allocation.sub_project_id == project_id
    ).all()
    return [enrich_allocation_response(a, db) for a in allocations]


@router.get("/by-employee/{employee_id}", response_model=List[dict])
def get_allocations_by_employee(employee_id: int, db: Session = Depends(get_db)):
    """Get all allocations for a specific employee."""
    allocations = db.query(Allocation).filter(
        Allocation.employee_id == employee_id
    ).all()
    return [enrich_allocation_response(a, db) for a in allocations]


@router.put("/{allocation_id}", response_model=dict)
def update_allocation(
    allocation_id: int,
    data: AllocationUpdate,
    db: Session = Depends(get_db)
):
    """Update an allocation with validation."""
    allocation = db.query(Allocation).filter(Allocation.id == allocation_id).first()
    
    if not allocation:
        raise HTTPException(status_code=404, detail="Allocation not found")
    
    # Validate time distribution if being updated
    new_hours = data.total_daily_hours or allocation.total_daily_hours or 8
    new_distribution = data.time_distribution if data.time_distribution is not None else allocation.time_distribution
    
    if new_distribution:
        time_check = validate_time_distribution(new_hours, new_distribution)
        if not time_check['is_valid']:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=time_check['message']
            )
    
    # Double-booking check if hours are changing
    if data.total_daily_hours:
        booking_check = check_double_booking(
            db=db,
            employee_id=data.employee_id or allocation.employee_id,
            new_hours=data.total_daily_hours,
            active_start=data.active_start_date or allocation.active_start_date,
            active_end=data.active_end_date or allocation.active_end_date,
            exclude_allocation_id=allocation_id
        )
        
        override_flag = data.override_flag if data.override_flag is not None else allocation.override_flag
        if booking_check.get('is_overbooked') and not override_flag:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": booking_check['message'],
                    "requires_override": True,
                    "booking_details": booking_check
                }
            )

    # Leave-overlap check on update: informational only.
    resolved_employee_id = data.employee_id or allocation.employee_id
    resolved_start = data.active_start_date if data.active_start_date is not None else allocation.active_start_date
    resolved_end   = data.active_end_date   if data.active_end_date   is not None else allocation.active_end_date
    leave_check = check_leave_conflict(
        db=db,
        employee_id=resolved_employee_id,
        alloc_start=resolved_start,
        alloc_end=resolved_end,
    )

    old_sub_project_id = allocation.sub_project_id

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(allocation, key, value)

    db.flush()

    # Sync allocated_employees count from actual records for affected projects
    new_sub_project_id = allocation.sub_project_id
    affected_project_ids = {old_sub_project_id}
    if new_sub_project_id != old_sub_project_id:
        affected_project_ids.add(new_sub_project_id)

    for pid in affected_project_ids:
        project = db.query(Project).filter(Project.id == pid).first()
        if project:
            actual_count = db.query(Allocation).filter(
                Allocation.sub_project_id == pid
            ).count()
            project.allocated_employees = actual_count

    db.commit()
    db.refresh(allocation)

    response = enrich_allocation_response(allocation, db)
    if leave_check["has_conflict"]:
        response["leave_warning"] = {
            "message": leave_check["message"],
            "excluded_leaves": leave_check["conflicting_leaves"],
        }
    return response


@router.delete("/{allocation_id}")
def delete_allocation(allocation_id: int, db: Session = Depends(get_db)):
    """Delete an allocation."""
    allocation = db.query(Allocation).filter(Allocation.id == allocation_id).first()
    
    if not allocation:
        raise HTTPException(status_code=404, detail="Allocation not found")
    
    sub_project_id = allocation.sub_project_id
    project = db.query(Project).filter(Project.id == sub_project_id).first()
    previous_count = 0
    if project:
        previous_count = db.query(Allocation).filter(
            Allocation.sub_project_id == sub_project_id
        ).count()
    db.delete(allocation)
    db.flush()

    # Sync project allocated_employees count from actual allocation records
    actual_count = 0
    if project:
        actual_count = db.query(Allocation).filter(
            Allocation.sub_project_id == sub_project_id
        ).count()
        project.allocated_employees = actual_count

    db.commit()

    try:
        _send_employee_allocation_removed_notification(db, allocation, project)
    except Exception:
        pass

    try:
        _notify_project_team_target_change(
            db,
            project,
            old_allocation_count=previous_count,
            new_allocation_count=actual_count,
            excluded_employee_ids={allocation.employee_id},
        )
    except Exception:
        pass

    return {"message": "Allocation removed"}

