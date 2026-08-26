from fastapi import APIRouter, Depends, HTTPException, Request, Query, status
from app.services.auth_service import get_current_user, has_team_read, require_role
from app.services import audit_service, project_scope
from app.models.user import User
from sqlalchemy.orm import Session
from typing import List, Optional

from app.db.database import get_db
from app.models.allocation import Allocation
from app.models.project import SubProject, Project  # SubProject with Project alias
from app.models.employee import Employee
from app.models.parent_project import MainProject
from app.models.sub_project import SubProject as HierarchySubProject
from datetime import date as date_cls
from app.models.leave import Leave
from app.models.wfh import WFHRequest
from app.schemas.allocation import (
    AllocationCreate, 
    AllocationUpdate, 
    AllocationResponse,
    AllocationValidationRequest,
    AllocationValidationResponse,
    EmployeeAllocationStatus,
    AllocationsPageResponse,
    ProjectAllocationRow,
    AllocatedEmployeePreview,
    ProjectAllocationDetailResponse,
    ProjectAllocationDetailItem,
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

TEAM_LEAD_TAG = "Team Lead"  # matches TEAM_LEAD_ROLE_TAG in sub_projects.py / TEAM_LEAD_TAG in frontend


def sync_project_allocations(db, project_id: int):
    from app.models.project import Project
    from app.models.allocation import Allocation
    from app.services import project_scope
    from app.api.projects import _autonex_headcount

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project: return

    allocs = db.query(Allocation).filter(
        Allocation.sub_project_id == project_id,
        Allocation.is_active == True
    ).all()

    project.allocated_employees = len(allocs)

    lead_count = 0
    pm_ids = set()

    for a in allocs:
        if a.role_tags and "Team Lead" in a.role_tags:
            lead_count += 1
        elif project_scope.escalates_to_admin(db, a.employee_id):
            pm_ids.add(a.employee_id)

    project.team_lead_count = lead_count
    project.team_manager_count = len(pm_ids)
    
    # Strictly sync assigned_employee_ids to the active PM allocations
    project.assigned_employee_ids = list(pm_ids)
    
    project.required_manpower = _autonex_headcount(project)

router = APIRouter(prefix="/api/allocations", tags=["Allocations"], dependencies=[Depends(get_current_user)])

def _resolve_pm_ids(project: Project, main_project_map: dict, employee_map: dict) -> list[int]:
    """A project's own PMs, falling back to its parent's program managers.
    Archived employees never count as a filled PM slot.
    """
    def is_stale(eid: int) -> bool:
        emp = employee_map.get(eid)
        return emp is None or emp.status == "archived"

    ids = [i for i in (project.assigned_employee_ids or []) if not is_stale(i)]
    if ids:
        return ids

    mp = main_project_map.get(getattr(project, "main_project_id", None))
    if mp:
        fallback = getattr(mp, "program_manager_ids", None) or (
            [mp.program_manager_id] if getattr(mp, "program_manager_id", None) else []
        )
        return [i for i in fallback if not is_stale(i)]
    return []


@router.get("/page", response_model=AllocationsPageResponse)
def get_allocations_page(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    search: Optional[str] = Query(None, max_length=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm", "team_lead")),
):
    """
    Server-side paginated + pre-aggregated replacement for building the whole
    Allocations board in the browser. Every batch query below is scoped to the
    projects that exist at all (cheap column-only reads); the per-row math is
    plain Python over already-fetched, already-narrow data — never a query per
    project, and never the full allocation/employee/leave/WFH tables loaded
    into React just to be summarised.
    """
    today = date_cls.today()

    # 1) Same visibility rules as GET /api/sub-projects.
    all_projects = db.query(Project).order_by(Project.id.asc()).all()
    if current_user.role in ("pm", "hr", "team_lead") and not project_scope.has_full_access(current_user):
        all_projects = [p for p in all_projects if project_scope.can_act_on_project(db, current_user, p)]

    if not all_projects:
        return AllocationsPageResponse(items=[], page=page, page_size=page_size, total_items=0, total_pages=0)

    project_ids = [p.id for p in all_projects]

    # 2) One query for every allocation on these projects (no N+1).
    all_allocs = db.query(Allocation).filter(Allocation.sub_project_id.in_(project_ids)).all()
    allocs_by_project: dict[int, list[Allocation]] = {}
    for a in all_allocs:
        allocs_by_project.setdefault(a.sub_project_id, []).append(a)

    # 3) Roster + parent-project lookups, batched.
    employee_ids_involved = {a.employee_id for a in all_allocs}
    for p in all_projects:
        employee_ids_involved.update(p.assigned_employee_ids or [])
    employees = (
        db.query(Employee).filter(Employee.id.in_(employee_ids_involved)).all()
        if employee_ids_involved else []
    )
    employee_map = {e.id: e for e in employees}

    main_project_ids = {p.main_project_id for p in all_projects if getattr(p, "main_project_id", None)}
    main_projects = (
        db.query(MainProject).filter(MainProject.id.in_(main_project_ids)).all()
        if main_project_ids else []
    )
    main_project_map = {mp.id: mp for mp in main_projects}

    def is_stale(eid: int) -> bool:
        emp = employee_map.get(eid)
        return emp is None or emp.status == "archived"

    # 4) Today's leave / WFH, batched once for every employee involved.
    leave_rows = (
        db.query(Leave)
        .filter(
            Leave.employee_id.in_(employee_ids_involved),
            Leave.status == "approved",
            Leave.start_date <= today,
            Leave.end_date >= today,
        )
        .all() if employee_ids_involved else []
    )
    on_leave_today = {l.employee_id for l in leave_rows}

    wfh_rows = (
        db.query(WFHRequest)
        .filter(
            WFHRequest.employee_id.in_(employee_ids_involved),
            WFHRequest.status == "approved",
            WFHRequest.wfh_date == today,
        )
        .all() if employee_ids_involved else []
    )
    wfh_today = {w.employee_id for w in wfh_rows}

    def is_wfh_today(emp: Employee) -> bool:
        return emp.id in wfh_today

    # 5) Build one row per project + a search blob kept OUT of the response.
    built: list[tuple[ProjectAllocationRow, str]] = []
    for project in all_projects:
        allocs = allocs_by_project.get(project.id, [])
        if not allocs and not (project.required_manpower or 0):
            continue  # mirrors the old client filter: only "active" rows

        pm_ids = _resolve_pm_ids(project, main_project_map, employee_map)
        lead_ids = list({
            a.employee_id for a in allocs
            if a.role_tags and TEAM_LEAD_TAG in a.role_tags and not is_stale(a.employee_id)
        })
        assigned_ids = set(pm_ids) | set(lead_ids)

        def sort_key(a: Allocation):
            emp = employee_map.get(a.employee_id)
            return (
                0 if a.employee_id in pm_ids else 1,
                0 if a.employee_id in lead_ids else 1,
                (emp.name if emp else "").lower(),
            )

        stale_count = wfo = wfh_c = on_leave = 0
        preview: list[AllocatedEmployeePreview] = []
        name_blob_parts = [project.name]

        for a in sorted(allocs, key=sort_key):
            stale = is_stale(a.employee_id)
            emp = employee_map.get(a.employee_id)
            name = emp.name if emp else "Former employee"
            name_blob_parts.append(name)

            if stale:
                stale_count += 1
            else:
                assigned_ids.add(a.employee_id)
                if a.employee_id in on_leave_today:
                    on_leave += 1
                elif is_wfh_today(emp):
                    wfh_c += 1
                else:
                    wfo += 1

            if len(preview) < 6:
                preview.append(AllocatedEmployeePreview(
                    allocation_id=a.id,
                    employee_id=a.employee_id,
                    name=name,
                    avatar_url=(emp.avatar_url if emp else None),
                    is_pm=a.employee_id in pm_ids,
                    is_lead=a.employee_id in lead_ids,
                    stale=stale,
                ))

        requested_manpower = (
            (project.autonex_annotators or 0)
            + (project.autonex_reviewers or 0)
            + (project.others_count or 0)
            + (project.developers_count or 0)
        )

        row = ProjectAllocationRow(
            project_id=project.id,
            project_name=project.name,
            project_type=project.project_type,
            required_manpower=project.required_manpower or 0,
            pm_slots=len(pm_ids),
            lead_slots=len(lead_ids),
            requested_manpower=requested_manpower,
            assigned_manpower=len(assigned_ids),
            stale_count=stale_count,
            wfo_count=wfo,
            wfh_count=wfh_c,
            on_leave_count=on_leave,
            allocated_preview=preview,
            total_allocated_count=len(allocs),
        )
        built.append((row, " ".join(name_blob_parts).lower()))

    # 6) Search (project name OR any allocated employee's name — not just the preview).
    if search and search.strip():
        term = search.strip().lower()
        built = [(r, blob) for (r, blob) in built if term in blob]

    total_items = len(built)
    total_pages = (total_items + page_size - 1) // page_size if page_size else 0
    start = (page - 1) * page_size
    page_rows = [r for (r, _blob) in built[start:start + page_size]]

    return AllocationsPageResponse(
        items=page_rows, page=page, page_size=page_size,
        total_items=total_items, total_pages=total_pages,
    )


@router.get("/project/{project_id}/detail", response_model=ProjectAllocationDetailResponse)
def get_project_allocation_detail(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm", "team_lead")),
):
    """Full per-employee allocation list for ONE project. Fetched on demand
    (row expand / Create-Allocation modal / Edit modal) instead of every
    project's roster being present in memory on every page load."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_scope.require_project_scope(
        db, current_user, project, action="view this project's allocations"
    )

    allocs = db.query(Allocation).filter(Allocation.sub_project_id == project_id).all()
    employee_ids = {a.employee_id for a in allocs} | set(project.assigned_employee_ids or [])
    employees = db.query(Employee).filter(Employee.id.in_(employee_ids)).all() if employee_ids else []
    employee_map = {e.id: e for e in employees}

    pm_ids = set(project.assigned_employee_ids or [])
    if not pm_ids and getattr(project, "main_project_id", None):
        mp = db.query(MainProject).filter(MainProject.id == project.main_project_id).first()
        if mp:
            pm_ids = set(getattr(mp, "program_manager_ids", None) or (
                [mp.program_manager_id] if getattr(mp, "program_manager_id", None) else []
            ))

    items = []

    today = date_cls.today()
    leaves = db.query(Leave).filter(
        Leave.employee_id.in_(employee_ids),
        Leave.status == "approved",
        Leave.start_date <= today,
        Leave.end_date >= today
    ).all() if employee_ids else []
    on_leave_ids = {leave.employee_id for leave in leaves}

    wfhs = db.query(WFHRequest).filter(
        WFHRequest.employee_id.in_(employee_ids),
        WFHRequest.status == "approved",
        WFHRequest.wfh_date <= today,
        (WFHRequest.end_date >= today) | (WFHRequest.end_date.is_(None))
    ).all() if employee_ids else []
    wfh_ids = {wfh.employee_id for wfh in wfhs}

    for a in allocs:
        emp = employee_map.get(a.employee_id)
        stale = emp is None or emp.status == "archived"
        
        is_wfh = emp and emp.id in wfh_ids
        location = "WFH" if is_wfh else ("WFO" if emp else None)

        items.append(ProjectAllocationDetailItem(
            allocation_id=a.id,
            employee_id=a.employee_id,
            name=(emp.name if emp else "Former employee"),
            email=(emp.email if emp else None),
            avatar_url=(emp.avatar_url if emp else None),
            designation=(emp.designation if emp else None),
            location=location,
            is_on_leave=bool(emp and emp.id in on_leave_ids),
            total_daily_hours=a.total_daily_hours,
            role_tags=a.role_tags or [],
            is_pm=a.employee_id in pm_ids,
            is_lead=bool(a.role_tags and TEAM_LEAD_TAG in a.role_tags),
            stale=stale,
        ))

    return ProjectAllocationDetailResponse(
        project_id=project.id,
        project_name=project.name,
        required_manpower=project.required_manpower or 0,
        items=items,
    )


@router.get("/employee-projects", response_model=dict)
def get_employee_current_projects(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm", "team_lead")),
):
    """employee_id -> [{project_id, project_name, hours}] for every active
    allocation. Used only to tag 'Other Project' in the Create-Allocation
    modal — fetched once when that modal opens, not on every page load."""
    allocs = db.query(Allocation).filter(Allocation.is_active == True).all()
    if not allocs:
        return {}
    project_ids = list({a.sub_project_id for a in allocs})
    projects = db.query(Project).filter(Project.id.in_(project_ids)).all()
    project_map = {p.id: p for p in projects}

    result: dict[str, list[dict]] = {}
    for a in allocs:
        proj = project_map.get(a.sub_project_id)
        if not proj:
            continue
        result.setdefault(str(a.employee_id), []).append({
            "project_id": proj.id,
            "project_name": proj.name,
            "hours": a.total_daily_hours or 8,
        })
    return result

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


def _allocation_to_dict(
    allocation: Allocation,
    employee: Optional[Employee] = None,
    sub_project: Optional[SubProject] = None,
) -> dict:
    """Single source of truth for the allocation response shape."""
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
        "sub_project_name": sub_project.name if sub_project else None,
    }


def enrich_allocation_response(allocation: Allocation, db: Session) -> dict:
    """Add employee and sub-project names (single-row path; does its own lookups)."""
    employee = db.query(Employee).filter(Employee.id == allocation.employee_id).first()
    sub_project = db.query(SubProject).filter(SubProject.id == allocation.sub_project_id).first()
    return _allocation_to_dict(allocation, employee, sub_project)


@router.post("/validate", response_model=AllocationValidationResponse, dependencies=[Depends(require_role("admin", "pm"))])
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
def create_allocation(
    data: AllocationCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    """Create a new allocation with validation."""
    # Staffing a project is the PM's call, so scope on the target project rather than on
    # the employee: the person being added is by definition not on it yet.
    project = db.query(Project).filter(Project.id == data.sub_project_id).first()
    if project and project.project_status == "archived":
        raise HTTPException(status_code=400, detail="Cannot allocate employees to an archived project.")

    project_scope.require_project_scope(
        db,
        current_user,
        project,
        action="allocate people to this project",
    )

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

    allocation = Allocation(**data.model_dump())
    db.add(allocation)
    db.flush()  # Flush to include the new allocation in the count query

    # Sync project allocated_employees and role counts dynamically
    sync_project_allocations(db, data.sub_project_id)

    # Count active allocations on this project (includes the one just flushed)
    actual_count = db.query(Allocation).filter(
        Allocation.sub_project_id == data.sub_project_id,
        Allocation.is_active == True,
    ).count()

    allocated_employee = db.query(Employee).filter(Employee.id == allocation.employee_id).first()
    allocated_name = allocated_employee.name if allocated_employee else None
    audit_service.record(
        db,
        actor=current_user,
        action="allocation.created",
        category="Allocations",
        action_type="Created",
        entity_type="allocation",
        entity_id=allocation.id,
        entity_name=allocated_name,
        subject_employee_id=allocation.employee_id,
        subject_name=allocated_name,
        details=audit_service.changes(
            audit_service.field_diff("Project", None, project.name if project else f"#{data.sub_project_id}"),
            audit_service.field_diff("Daily hours", None, allocation.total_daily_hours),
            audit_service.field_diff(
                "Active period", None,
                f"{allocation.active_start_date} → {allocation.active_end_date}",
            ),
            # Worth recording explicitly: an overbooking that was consciously forced
            # through is exactly the decision someone will later ask about.
            audit_service.field_diff("Overbooking override", None, True if data.override_flag else None),
        ),
        summary=(
            f"Assigned {allocated_name or 'employee'} to "
            f"{project.name if project else 'a project'}"
            + (f" at {allocation.total_daily_hours}h/day" if allocation.total_daily_hours else "")
        ),
        request=http_request,
    )

    db.commit()
    db.refresh(allocation)

    # Notify only the newly allocated employee. The whole-team
    # "target changed" broadcast was intentionally removed so that adding a
    # member doesn't spam everyone already on the project.
    try:
        _send_employee_allocation_notification(db, allocation, project, actual_count)
    except Exception:
        pass

    response = enrich_allocation_response(allocation, db)
    if leave_check["has_conflict"]:
        response["leave_warning"] = {
            "message": leave_check["message"],
            "excluded_leaves": leave_check["conflicting_leaves"],
        }
    return response


@router.get("/slim")
def get_allocations_slim(db: Session = Depends(get_db)):
    """Ultra-lightweight endpoint for popovers and UI counting."""
    allocs = db.query(Allocation.id, Allocation.employee_id, Allocation.sub_project_id, Allocation.role_tags).filter(
        Allocation.is_active == True
    ).all()
    return [{"id": a[0], "employee_id": a[1], "sub_project_id": a[2], "role_tags": a[3]} for a in allocs]

@router.get("", response_model=List[dict], dependencies=[Depends(require_role("admin", "pm"))])
def get_allocations(db: Session = Depends(get_db)):
    """Get all allocations with enriched data (optimized to avoid N+1 queries)."""
    allocations = db.query(Allocation).filter(Allocation.is_active == True).all()
    
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
        result.append(_allocation_to_dict(allocation, emp, proj))
    
    return result


@router.get("/employee-status", response_model=dict, dependencies=[Depends(require_role("admin", "pm"))])
def get_employee_allocation_status(
    active_only: bool = True,
    db: Session = Depends(get_db)
):
    """
    Get allocation status for all employees, grouped by status.
    Used for UI filtering (Unallocated/Partial/Full).
    """
    return get_all_employees_allocation_status(db, active_only)


@router.get("/by-project/{project_id}", response_model=List[dict], dependencies=[Depends(require_role("admin", "pm"))])
def get_allocations_by_project(project_id: int, db: Session = Depends(get_db)):
    """Get all allocations for a specific project."""
    allocations = db.query(Allocation).filter(
        Allocation.sub_project_id == project_id,
        Allocation.is_active == True
    ).all()
    return [enrich_allocation_response(a, db) for a in allocations]


@router.get("/by-employee/{employee_id}", response_model=List[dict])
def get_allocations_by_employee(
    employee_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all allocations for a specific employee."""
    if not has_team_read(current_user):
        is_self = current_user.employee_id == employee_id
        if not is_self:
            employee = db.query(Employee).filter(Employee.id == employee_id).first()
            if not employee or current_user.email != employee.email:
                raise HTTPException(status_code=403, detail="Access denied")
    allocations = db.query(Allocation).filter(
        Allocation.employee_id == employee_id,
        Allocation.is_active == True
    ).all()
    return [enrich_allocation_response(a, db) for a in allocations]


@router.put("/{allocation_id}", response_model=dict)
def update_allocation(
    allocation_id: int,
    data: AllocationUpdate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    """Update an allocation with validation."""
    allocation = db.query(Allocation).filter(Allocation.id == allocation_id).first()
    
    if not allocation:
        raise HTTPException(status_code=404, detail="Allocation not found")

    # Scope on the project the allocation is on now...
    old_project = db.query(Project).filter(Project.id == allocation.sub_project_id).first()
    if old_project and old_project.project_status == "archived":
        raise HTTPException(status_code=400, detail="Cannot edit allocations for an archived project.")

    project_scope.require_project_scope(
        db,
        current_user,
        old_project,
        action="change allocations on this project",
    )
    # ...and, when the allocation is being moved, on the destination too. Checking only
    # the source would let a PM push someone onto a project they do not manage.
    if data.sub_project_id and data.sub_project_id != allocation.sub_project_id:
        new_project = db.query(Project).filter(Project.id == data.sub_project_id).first()
        if new_project and new_project.project_status == "archived":
            raise HTTPException(status_code=400, detail="Cannot move an allocation to an archived project.")

        project_scope.require_project_scope(
            db,
            current_user,
            new_project,
            action="move allocations onto this project",
        )

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
    
    # Double-booking check — always re-validate on update.
    # Moving an allocation to a new project / date range / employee without
    # changing hours can still create overlaps that the previous range avoided.
    # (Previously the check only ran when data.total_daily_hours was truthy.)
    resolved_employee_id = data.employee_id if data.employee_id is not None else allocation.employee_id
    resolved_hours = (
        data.total_daily_hours
        if data.total_daily_hours is not None
        else (allocation.total_daily_hours or 8)
    )
    resolved_start = (
        data.active_start_date
        if data.active_start_date is not None
        else allocation.active_start_date
    )
    resolved_end = (
        data.active_end_date
        if data.active_end_date is not None
        else allocation.active_end_date
    )

    booking_check = check_double_booking(
        db=db,
        employee_id=resolved_employee_id,
        new_hours=resolved_hours,
        active_start=resolved_start,
        active_end=resolved_end,
        exclude_allocation_id=allocation_id,
    )

    override_flag = (
        data.override_flag
        if data.override_flag is not None
        else allocation.override_flag
    )
    if booking_check.get("is_overbooked") and not override_flag:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": booking_check["message"],
                "requires_override": True,
                "booking_details": booking_check,
            },
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

    # Snapshot every field the caller is about to overwrite, before overwriting it.
    # Read after the setattr loop and the "from" side of each diff would already be
    # the new value.
    changed_keys = list(data.model_dump(exclude_unset=True).keys())
    before_values = {key: getattr(allocation, key, None) for key in changed_keys}
    old_employee_id = allocation.employee_id

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
                Allocation.sub_project_id == pid,
                Allocation.is_active == True
            ).count()
            project.allocated_employees = actual_count

    # Human-readable labels for the fields people actually care about; anything else
    # falls back to its raw column name rather than being dropped silently.
    FIELD_LABELS = {
        "employee_id": "Employee",
        "sub_project_id": "Project",
        "total_daily_hours": "Daily hours",
        "active_start_date": "Start date",
        "active_end_date": "End date",
        "time_distribution": "Time split",
        "override_flag": "Overbooking override",
    }
    updated_employee = db.query(Employee).filter(Employee.id == allocation.employee_id).first()
    updated_name = updated_employee.name if updated_employee else None
    audit_service.record(
        db,
        actor=current_user,
        action="allocation.updated",
        category="Allocations",
        action_type="Updated",
        entity_type="allocation",
        entity_id=allocation.id,
        entity_name=updated_name,
        subject_employee_id=allocation.employee_id,
        subject_name=updated_name,
        details=audit_service.changes(
            *[
                audit_service.field_diff(
                    FIELD_LABELS.get(key, key),
                    before_values.get(key),
                    getattr(allocation, key, None),
                )
                for key in changed_keys
            ]
        ),
        summary=(
            f"Updated allocation for {updated_name or 'employee'}"
            + (
                " — reassigned to a different project"
                if allocation.sub_project_id != old_sub_project_id
                else ""
            )
            + (
                " — moved to a different employee"
                if allocation.employee_id != old_employee_id
                else ""
            )
        ),
        request=http_request,
    )

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
def delete_allocation(
    allocation_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    """Delete an allocation."""
    allocation = db.query(Allocation).filter(Allocation.id == allocation_id).first()

    if not allocation:
        raise HTTPException(status_code=404, detail="Allocation not found")

    sub_project_id = allocation.sub_project_id
    project = db.query(Project).filter(Project.id == sub_project_id).first()

    project_scope.require_project_scope(
        db, current_user, project, action="remove allocations from this project"
    )

    # Captured before the delete — afterwards there is no row left to describe.
    removed_employee = db.query(Employee).filter(Employee.id == allocation.employee_id).first()
    removed_name = removed_employee.name if removed_employee else None
    audit_service.record(
        db,
        actor=current_user,
        action="allocation.removed",
        category="Allocations",
        action_type="Deleted",
        entity_type="allocation",
        entity_id=allocation.id,
        entity_name=removed_name,
        subject_employee_id=allocation.employee_id,
        subject_name=removed_name,
        details=audit_service.changes(
            audit_service.field_diff("Project", project.name if project else f"#{sub_project_id}", None),
            audit_service.field_diff("Daily hours", allocation.total_daily_hours, None),
        ),
        summary=(
            f"Removed {removed_name or 'employee'} from "
            f"{project.name if project else 'a project'}"
        ),
        request=http_request,
    )

    db.delete(allocation)
    db.flush()

    # Sync project allocated_employees and role counts dynamically
    if project:
        sync_project_allocations(db, sub_project_id)

    db.commit()

    # Notify only the removed employee. The whole-team "target changed"
    # broadcast was intentionally removed so removing a member doesn't spam
    # everyone still on the project.
    try:
        _send_employee_allocation_removed_notification(db, allocation, project)
    except Exception:
        pass

    return {"message": "Allocation removed"}

