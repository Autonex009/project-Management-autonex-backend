"""
Allocation Validation Service
Implements business logic for allocation validation including:
- Sum-Zero validation (time distribution must equal total hours)
- Double-booking detection across projects
- Leave conflict detection (approved leave blocks assignment)
"""
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from datetime import date
from typing import List, Optional, Dict, Any

from app.models.allocation import Allocation
from app.models.employee import Employee
from app.models.leave import Leave


class AllocationValidationError(Exception):
    """Custom exception for allocation validation failures."""
    def __init__(self, message: str, error_type: str, details: dict = None):
        self.message = message
        self.error_type = error_type
        self.details = details or {}
        super().__init__(self.message)


def check_leave_conflict(
    db: Session,
    employee_id: int,
    alloc_start: Optional[date],
    alloc_end: Optional[date],
) -> Dict[str, Any]:
    """
    Leave-Conflict Check:
    An employee with an *approved* leave that overlaps the allocation date range
    must not be assigned to a project during that period.

    Only approved leaves are considered; pending and rejected leaves are ignored.
    If either alloc_start or alloc_end is None the check is skipped (open-ended
    allocations cannot be compared to a leave date range).

    Args:
        db:           Database session
        employee_id:  Employee to check
        alloc_start:  Proposed allocation start date (inclusive)
        alloc_end:    Proposed allocation end date (inclusive)

    Returns:
        {
            'has_conflict': bool,
            'employee_id': int,
            'conflicting_leaves': [
                {'leave_id': int, 'start_date': str, 'end_date': str, 'leave_type': str}
            ],
            'message': str
        }
    """
    if alloc_start is None or alloc_end is None:
        return {
            "has_conflict": False,
            "employee_id": employee_id,
            "conflicting_leaves": [],
            "message": "No date range provided; leave conflict check skipped",
        }

    # Find approved leaves whose date range overlaps [alloc_start, alloc_end].
    # Two ranges [A,B] and [C,D] overlap when A <= D and C <= B.
    conflicting = (
        db.query(Leave)
        .filter(
            Leave.employee_id == employee_id,
            Leave.status == "approved",
            Leave.start_date <= alloc_end,
            Leave.end_date >= alloc_start,
        )
        .all()
    )

    if not conflicting:
        return {
            "has_conflict": False,
            "employee_id": employee_id,
            "conflicting_leaves": [],
            "message": "No leave conflicts found",
        }

    leave_details = [
        {
            "leave_id": lv.id,
            "start_date": lv.start_date.isoformat(),
            "end_date": lv.end_date.isoformat(),
            "leave_type": lv.leave_type or "unknown",
        }
        for lv in conflicting
    ]

    date_strs = ", ".join(
        f"{d['start_date']} to {d['end_date']}" for d in leave_details
    )
    message = (
        f"Employee {employee_id} is on approved leave during the allocation period "
        f"({date_strs}). Remove the leave or adjust the allocation dates."
    )

    return {
        "has_conflict": True,
        "employee_id": employee_id,
        "conflicting_leaves": leave_details,
        "message": message,
    }


def validate_time_distribution(
    total_daily_hours: int,
    time_distribution: Dict[str, int]
) -> Dict[str, Any]:
    """
    Sum-Zero Validation:
    Sum of time_distribution values must equal total_daily_hours.
    
    Returns:
        {
            'is_valid': bool,
            'total_hours': int,
            'distribution_sum': int,
            'difference': int,
            'message': str
        }
    """
    if not time_distribution:
        # If no distribution, it's valid (hours aren't split by role)
        return {
            'is_valid': True,
            'total_hours': total_daily_hours,
            'distribution_sum': 0,
            'difference': 0,
            'message': 'No time distribution specified'
        }
    
    distribution_sum = sum(time_distribution.values())
    difference = distribution_sum - total_daily_hours
    is_valid = difference == 0
    
    if not is_valid:
        if difference > 0:
            message = f"Time distribution ({distribution_sum}h) exceeds total daily hours ({total_daily_hours}h) by {difference}h"
        else:
            message = f"Time distribution ({distribution_sum}h) is less than total daily hours ({total_daily_hours}h) by {abs(difference)}h"
    else:
        message = "Time distribution is valid"
    
    return {
        'is_valid': is_valid,
        'total_hours': total_daily_hours,
        'distribution_sum': distribution_sum,
        'difference': difference,
        'message': message
    }


def check_double_booking(
    db: Session,
    employee_id: int,
    new_hours: int,
    active_start: Optional[date] = None,
    active_end: Optional[date] = None,
    exclude_allocation_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Double-Booking Check:
    Query all active allocations for employee across all projects.
    Check if ExistingHours + NewRequestHours > max_daily_capacity (usually 8).
    
    Args:
        db: Database session
        employee_id: Employee to check
        new_hours: Hours being requested in new/updated allocation
        active_start: Start date of new allocation (optional)
        active_end: End date of new allocation (optional)
        exclude_allocation_id: Allocation ID to exclude (for updates)
    
    Returns:
        {
            'is_overbooked': bool,
            'employee_name': str,
            'max_capacity': int,
            'existing_hours': int,
            'new_hours': int,
            'total_projected': int,
            'available_hours': int,
            'requires_override': bool,
            'existing_allocations': [...],
            'message': str
        }
    """
    # Get employee info
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        return {
            'is_overbooked': False,
            'error': f"Employee with ID {employee_id} not found",
            'requires_override': False
        }
    
    max_capacity = int(employee.working_hours_per_day or 8)
    
    # Build query for existing allocations
    query = db.query(Allocation).filter(Allocation.employee_id == employee_id)
    
    # Exclude current allocation if updating
    if exclude_allocation_id:
        query = query.filter(Allocation.id != exclude_allocation_id)
    
    # Filter by overlapping date range if provided
    if active_start and active_end:
        query = query.filter(
            or_(
                # Case 1: Existing allocation has no dates (always overlaps)
                and_(
                    Allocation.active_start_date.is_(None),
                    Allocation.active_end_date.is_(None)
                ),
                # Case 2: Existing allocation overlaps with new range
                and_(
                    Allocation.active_start_date <= active_end,
                    Allocation.active_end_date >= active_start
                ),
                # Case 3: Existing has start but no end
                and_(
                    Allocation.active_start_date.isnot(None),
                    Allocation.active_end_date.is_(None),
                    Allocation.active_start_date <= active_end
                ),
                # Case 4: Existing has end but no start
                and_(
                    Allocation.active_start_date.is_(None),
                    Allocation.active_end_date.isnot(None),
                    Allocation.active_end_date >= active_start
                )
            )
        )
    
    existing_allocations = query.all()
    
    # Calculate total existing hours
    existing_hours = sum(
        alloc.total_daily_hours or alloc.weekly_hours_allocated or 0
        for alloc in existing_allocations
    )
    
    total_projected = existing_hours + new_hours
    available_hours = max(0, max_capacity - existing_hours)
    is_overbooked = total_projected > max_capacity
    
    # Build allocation details
    allocation_details = []
    for alloc in existing_allocations:
        allocation_details.append({
            'allocation_id': alloc.id,
            'project_id': alloc.sub_project_id,
            'hours': alloc.total_daily_hours or alloc.weekly_hours_allocated or 0,
            'active_start': alloc.active_start_date.isoformat() if alloc.active_start_date else None,
            'active_end': alloc.active_end_date.isoformat() if alloc.active_end_date else None
        })
    
    if is_overbooked:
        overage = total_projected - max_capacity
        message = f"{employee.name} would be overbooked by {overage}h (Total: {total_projected}h, Capacity: {max_capacity}h)"
    else:
        message = f"{employee.name} has {available_hours}h available"
    
    return {
        'is_overbooked': is_overbooked,
        'employee_id': employee_id,
        'employee_name': employee.name,
        'max_capacity': max_capacity,
        'existing_hours': existing_hours,
        'new_hours': new_hours,
        'total_projected': total_projected,
        'available_hours': available_hours,
        'requires_override': is_overbooked,
        'existing_allocations': allocation_details,
        'message': message
    }


def get_employee_allocation_status(
    db: Session,
    employee_id: int
) -> Dict[str, Any]:
    """
    Get current allocation status for an employee.
    Used for UI grouping (Unallocated/Partial/Full).
    
    Returns:
        {
            'employee_id': int,
            'employee_name': str,
            'status': 'unallocated' | 'partial' | 'full',
            'total_allocated': int,
            'max_capacity': int,
            'utilization_percent': float
        }
    """
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        return None
    
    allocations = db.query(Allocation).filter(
        Allocation.employee_id == employee_id
    ).all()
    
    max_capacity = int(employee.working_hours_per_day or 8)
    total_allocated = sum(
        alloc.total_daily_hours or alloc.weekly_hours_allocated or 0
        for alloc in allocations
    )
    
    utilization_percent = (total_allocated / max_capacity * 100) if max_capacity > 0 else 0
    
    # Determine status
    if total_allocated == 0:
        status = 'unallocated'
    elif total_allocated >= max_capacity:
        status = 'full'
    else:
        status = 'partial'
    
    return {
        'employee_id': employee_id,
        'employee_name': employee.name,
        'status': status,
        'total_allocated': total_allocated,
        'max_capacity': max_capacity,
        'utilization_percent': round(utilization_percent, 1)
    }


def get_all_employees_allocation_status(
    db: Session,
    active_only: bool = True
) -> List[Dict[str, Any]]:
    """
    Get allocation status for all employees.
    Returns employees grouped by allocation status.
    """
    query = db.query(Employee)
    if active_only:
        query = query.filter(Employee.status == 'active')
    
    employees = query.all()
    
    results = {
        'unallocated': [],
        'partial': [],
        'full': []
    }
    
    for emp in employees:
        status = get_employee_allocation_status(db, emp.id)
        if status:
            results[status['status']].append(status)
    
    return results
