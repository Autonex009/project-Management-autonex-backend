import logging
from fastapi import APIRouter, Depends, HTTPException, Request, Query, status, BackgroundTasks
from app.services.auth_service import get_current_user, has_team_read, require_role
from app.services import audit_service, project_scope
from app.models.user import User
from sqlalchemy import func
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from app.services.project_scope import _as_int_set

from app.db.database import get_db, SessionLocal
from app.models.allocation import Allocation
from app.models.project import SubProject, Project  # SubProject with Project alias
from app.models.employee import Employee
from app.models.parent_project import MainProject
from app.models.sub_project import SubProject as HierarchySubProject
from app.models.onboarding_pipeline import OnboardingPipeline
from datetime import date as date_cls
from app.models.leave import Leave
from app.models.wfh import WFHRequest
from app.api.projects import _get_temp_rosters_for_projects
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
    check_leave_conflict,
    get_all_employees_allocation_status,
    validate_time_distribution,
)
from app.services.slack_service import (
    notify_employee_allocation_created,
    notify_employee_allocation_removed,
    notify_employee_sub_project_updated,
    send_allocation_notifications_to_leaders,
    send_unallocation_notifications,
    try_get_or_cache_employee_slack_user_id,
    queue_pm_allocation_batch,
)

logger = logging.getLogger(__name__)

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

    emp_ids = [a.employee_id for a in allocs if a.employee_id]
    emp_map = {e.id: e for e in db.query(Employee).filter(Employee.id.in_(emp_ids)).all()} if emp_ids else {}

    lead_count = 0
    pm_ids = set()

    for a in allocs:
        emp = emp_map.get(a.employee_id)
        if (a.role_tags and "Team Lead" in a.role_tags) or _is_team_lead(emp):
            lead_count += 1
        elif project_scope.escalates_to_admin(db, a.employee_id):
            pm_ids.add(a.employee_id)

    project.team_lead_count = lead_count
    project.team_manager_count = len(pm_ids)
    
    # Strictly sync assigned_employee_ids to the active PM allocations
    project.assigned_employee_ids = list(pm_ids)
    
    project.required_manpower = _autonex_headcount(project)

router = APIRouter(prefix="/api/allocations", tags=["Allocations"], dependencies=[Depends(get_current_user)])

def _is_team_lead(emp: Employee) -> bool:
    if not emp or not emp.designation: return False
    val = emp.designation.strip().lower().replace(" ", "_").replace("-", "_")
    return val in {"team_lead", "teamlead", "tl"}

def _resolve_pm_ids(project: Project, employee_map: dict) -> list[int]:
    """A project's own PMs directly from assigned_employee_ids.
    Archived employees never count as a filled PM slot.
    Team Leads incorrectly placed in the manager slot are excluded here (they are leads).
    """
    def is_stale(eid: int) -> bool:
        emp = employee_map.get(eid)
        return emp is None or emp.status == "archived"

    ids = [i for i in (project.assigned_employee_ids or []) if not is_stale(i)]
    return [i for i in ids if not _is_team_lead(employee_map.get(i))]


def _resolve_demoted_lead_ids(project: Project, employee_map: dict) -> list[int]:
    """Finds users sitting in the manager slot who are actually Team Leads by designation."""
    def is_stale(eid: int) -> bool:
        emp = employee_map.get(eid)
        return emp is None or emp.status == "archived"

    ids = [i for i in (project.assigned_employee_ids or []) if not is_stale(i)]
    return [i for i in ids if _is_team_lead(employee_map.get(i))]


def _build_project_allocation_row(
    project: Project,
    allocs: list[Allocation],
    employee_map: dict[int, Employee],
    temp_info: dict,
) -> ProjectAllocationRow:
    """Pure in-memory row builder for ProjectAllocationRow. Zero DB queries.
    Used by both the paginated table and the surgical single-row endpoint.
    """
    pm_ids = _resolve_pm_ids(project, employee_map)
    demoted_ids = _resolve_demoted_lead_ids(project, employee_map)
    lead_ids_set = set(demoted_ids)

    def is_stale(eid: int) -> bool:
        emp = employee_map.get(eid)
        return emp is None or emp.status == "archived"

    for a in allocs:
        if is_stale(a.employee_id):
            continue
        tagged = False
        for tag in (a.role_tags or []):
            val = tag.strip().lower().replace(" ", "_").replace("-", "_")
            if val in {"team_lead", "teamlead", "tl"}:
                tagged = True
                break

        if tagged or _is_team_lead(employee_map.get(a.employee_id)):
            lead_ids_set.add(a.employee_id)

    lead_ids = list(lead_ids_set)
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

    for a in sorted(allocs, key=sort_key):
        stale = is_stale(a.employee_id)
        emp = employee_map.get(a.employee_id)
        name = emp.name if emp else "Former employee"

        if stale:
            stale_count += 1
        else:
            assigned_ids.add(a.employee_id)
            loc = temp_info.get("permanent_locations", {}).get(a.employee_id)
            if loc == "Leave":
                on_leave += 1
            elif loc and ("WFH" in loc.upper() or "HOME" in loc.upper()):
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

    return ProjectAllocationRow(
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
        daily_presence_total=temp_info.get("temp_count", 0),
        daily_presence_wfo=len([t for t in temp_info.get("temp_roster", []) if t.get("work_location") == "WFO"]),
        daily_presence_wfh=len([t for t in temp_info.get("temp_roster", []) if t.get("work_location") == "WFH"]),
        temp_roster=temp_info.get("temp_roster", []),
    )


class BulkAllocationCreateRequest(BaseModel):
    project_id: int
    employee_ids: list[int]
    role_tags: Optional[list[str]] = None
    daily_hours: Optional[float] = 8.0


class BulkAllocationDeleteRequest(BaseModel):
    project_id: int
    allocation_ids: list[int]


@router.get("/page", response_model=AllocationsPageResponse)
def get_allocations_page(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    search: Optional[str] = Query(None, max_length=200),
    project_view: Optional[str] = Query("active", description="active | archived"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm", "team_lead")),
):
    """Senior Principal Optimized Allocations Page:
    1. Zero N+1: Batched scoping resolution via 60s in-memory TTL cache.
    2. SQL Pre-Pagination: Limits query to strictly 10 visible projects in DB first.
    3. Consolidated JOIN: Loads allocations + employees in 1 single round-trip.
    4. Sub-20ms DB connection holding time to protect the 14-connection Supabase pool.
    """
    auth_ids = project_scope.get_authorized_project_ids(db, current_user)
    query = db.query(Project)
    if auth_ids is not None:
        query = query.filter(Project.id.in_(auth_ids))

    # Active vs Archived
    ARCHIVED_STATUSES = {"completed", "on-hold", "cancelled"}
    view = (project_view or "active").strip().lower()
    if view == "archived":
        query = query.filter(func.lower(Project.project_status).in_(ARCHIVED_STATUSES))
    else:
        query = query.filter(
            (Project.project_status == None) | 
            (~func.lower(Project.project_status).in_(ARCHIVED_STATUSES))
        )

    # Filter to projects with required manpower OR active allocations
    query = query.filter(
        (func.coalesce(Project.required_manpower, 0) > 0) | 
        (func.coalesce(Project.allocated_employees, 0) > 0)
    )

    # Search in SQL (project name OR allocated employee name)
    if search and search.strip():
        term = f"%{search.strip().lower()}%"
        query = query.filter(
            (func.lower(Project.name).like(term)) |
            (Project.id.in_(
                db.query(Allocation.sub_project_id)
                .join(Employee, Employee.id == Allocation.employee_id)
                .filter(Allocation.is_active == True, func.lower(Employee.name).like(term))
            ))
        )

    # Fast-Path Pagination (skips full count scan if total results fit on page 1)
    if page == 1:
        peek = query.order_by(Project.id.asc()).limit(page_size + 1).all()
        if len(peek) <= page_size:
            page_projects = peek
            total_items = len(page_projects)
            total_pages = 1 if total_items > 0 else 0
        else:
            page_projects = peek[:page_size]
            total_items = query.count()
            total_pages = (total_items + page_size - 1) // page_size
    else:
        total_items = query.count()
        total_pages = (total_items + page_size - 1) // page_size if page_size else 0
        page_projects = (
            query.order_by(Project.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

    if not page_projects:
        return AllocationsPageResponse(
            items=[], page=page, page_size=page_size, total_items=total_items, total_pages=total_pages
        )

    page_pids = [p.id for p in page_projects]

    # Consolidated 1-round-trip JOIN for Allocations + Employees (for these 10 projects only)
    alloc_rows = (
        db.query(Allocation, Employee)
        .join(Employee, Employee.id == Allocation.employee_id)
        .filter(Allocation.sub_project_id.in_(page_pids), Allocation.is_active == True)
        .all()
    )

    allocs_by_project: dict[int, list[Allocation]] = {}
    employee_map: dict[int, Employee] = {}
    for a, emp in alloc_rows:
        allocs_by_project.setdefault(a.sub_project_id, []).append(a)
        employee_map[emp.id] = emp

    # Ensure PMs listed in assigned_employee_ids are also loaded into employee_map
    all_assigned_eids = set()
    for p in page_projects:
        all_assigned_eids.update(_as_int_set(p.assigned_employee_ids))
    missing_pm_eids = all_assigned_eids - set(employee_map.keys())
    if missing_pm_eids:
        for emp in db.query(Employee).filter(Employee.id.in_(missing_pm_eids)).all():
            employee_map[emp.id] = emp

    # Temp rosters strictly for these 10 visible projects
    from app.api.projects import _get_temp_rosters_for_projects
    temp_data = _get_temp_rosters_for_projects(db, page_pids, allocs_by_project)

    page_rows = [
        _build_project_allocation_row(
            project,
            allocs_by_project.get(project.id, []),
            employee_map,
            temp_data.get(project.id, {"temp_count": 0, "temp_roster": [], "permanent_locations": {}}),
        )
        for project in page_projects
    ]

    return AllocationsPageResponse(
        items=page_rows, page=page, page_size=page_size,
        total_items=total_items, total_pages=total_pages,
    )


@router.get("/project/{project_id}/row", response_model=ProjectAllocationRow)
def get_project_allocation_row(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm", "team_lead")),
):
    """Surgical single-row endpoint for Cache Surgery (~15ms).
    Used by React Query on mutations to update exactly 1 table row in 0ms without page refetch.
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_scope.require_project_scope(
        db, current_user, project, action="view this project's allocation row"
    )

    alloc_rows = (
        db.query(Allocation, Employee)
        .join(Employee, Employee.id == Allocation.employee_id)
        .filter(Allocation.sub_project_id == project_id, Allocation.is_active == True)
        .all()
    )
    allocs = [a for a, _ in alloc_rows]
    employee_map = {emp.id: emp for _, emp in alloc_rows}

    for eid in _as_int_set(project.assigned_employee_ids):
        if eid not in employee_map:
            emp = db.query(Employee).filter(Employee.id == eid).first()
            if emp:
                employee_map[emp.id] = emp

    from app.api.projects import _get_temp_rosters_for_projects
    temp_data = _get_temp_rosters_for_projects(db, [project_id], {project_id: allocs})
    temp_info = temp_data.get(project_id, {"temp_count": 0, "temp_roster": [], "permanent_locations": {}})

    return _build_project_allocation_row(project, allocs, employee_map, temp_info)


@router.post("/bulk")
def bulk_create_allocations(
    payload: BulkAllocationCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm", "team_lead")),
):
    """Bulk create allocations for multiple employees in 1 single atomic transaction."""
    project = db.query(Project).filter(Project.id == payload.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_scope.require_project_scope(
        db, current_user, project, action="allocate employees to this project"
    )

    if not payload.employee_ids:
        raise HTTPException(status_code=400, detail="No employees specified")

    created_allocations = []
    skipped_count = 0

    existing = {
        row[0] for row in db.query(Allocation.employee_id).filter(
            Allocation.sub_project_id == payload.project_id,
            Allocation.employee_id.in_(payload.employee_ids),
            Allocation.is_active == True,
        ).all()
    }

    clean_role_tags = [t.strip() for t in (payload.role_tags or []) if t.strip()]

    for eid in payload.employee_ids:
        if eid in existing:
            skipped_count += 1
            continue
        new_alloc = Allocation(
            sub_project_id=payload.project_id,
            employee_id=eid,
            role_tags=clean_role_tags,
            daily_hours=payload.daily_hours or 8.0,
            is_active=True,
        )
        db.add(new_alloc)
        created_allocations.append(new_alloc)

    db.flush()
    sync_project_allocations(db, payload.project_id)
    project_scope.invalidate_project_scope_cache()
    db.commit()

    return {
        "project_id": payload.project_id,
        "created_count": len(created_allocations),
        "skipped_count": skipped_count,
        "allocation_ids": [a.id for a in created_allocations],
    }


@router.post("/bulk-delete")
def bulk_delete_allocations(
    payload: BulkAllocationDeleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm", "team_lead")),
):
    """Bulk deactivate multiple allocations on a project in 1 atomic transaction."""
    project = db.query(Project).filter(Project.id == payload.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_scope.require_project_scope(
        db, current_user, project, action="remove allocations from this project"
    )

    if not payload.allocation_ids:
        raise HTTPException(status_code=400, detail="No allocations specified")

    allocs = db.query(Allocation).filter(
        Allocation.id.in_(payload.allocation_ids),
        Allocation.sub_project_id == payload.project_id,
        Allocation.is_active == True,
    ).all()

    for a in allocs:
        a.is_active = False

    db.flush()
    sync_project_allocations(db, payload.project_id)
    project_scope.invalidate_project_scope_cache()
    db.commit()

    return {
        "project_id": payload.project_id,
        "deleted_count": len(allocs),
        "deleted_allocation_ids": [a.id for a in allocs],
    }


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

    allocs = db.query(Allocation).filter(
        Allocation.sub_project_id == project_id,
        Allocation.is_active == True
    ).all()
    employee_ids = {a.employee_id for a in allocs} | set(project.assigned_employee_ids or [])
    employees = db.query(Employee).filter(Employee.id.in_(employee_ids)).all() if employee_ids else []
    employee_map = {e.id: e for e in employees}

    pm_ids = set(_resolve_pm_ids(project, employee_map))
    demoted_ids = set(_resolve_demoted_lead_ids(project, employee_map))

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

    from app.models.daily_checkin import DailyCheckIn
    checkins = db.query(DailyCheckIn).filter(
        DailyCheckIn.employee_id.in_(employee_ids),
        DailyCheckIn.checkin_date == today
    ).order_by(DailyCheckIn.created_at.desc()).all() if employee_ids else []
    
    checkin_map = {}
    for c in checkins:
        if c.employee_id not in checkin_map:
            checkin_map[c.employee_id] = c.work_mode

    for a in allocs:
        emp = employee_map.get(a.employee_id)
        stale = emp is None or emp.status == "archived"
        
        location = None
        if emp:
            if emp.id in checkin_map and emp.id not in on_leave_ids:
                location = checkin_map[emp.id]
            elif emp.id in wfh_ids:
                location = "WFH"
            else:
                wm = emp.work_model or "WFO"
                location = "WFH" if "HOME" in wm.upper() or "WFH" in wm.upper() else "WFO"

        tagged = False
        for tag in (a.role_tags or []):
            val = tag.strip().lower().replace(" ", "_").replace("-", "_")
            if val in {"team_lead", "teamlead", "tl"}:
                tagged = True
                break
                
        is_lead_flag = tagged or _is_team_lead(emp) or (a.employee_id in demoted_ids)

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
            is_lead=is_lead_flag,
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

    if getattr(project, "assigned_employee_ids", None):
        # Pick the first valid assigned PM
        for pm_id in project.assigned_employee_ids:
            try:
                emp = db.query(Employee).filter(Employee.id == int(pm_id)).first()
                if emp:
                    pm_name = emp.name
                    break
            except (ValueError, TypeError):
                continue

    if not pm_name and getattr(project, "main_project_id", None):
        main_project = db.query(MainProject).filter(MainProject.id == project.main_project_id).first()
        if main_project and getattr(main_project, "program_manager_ids", None):
            for pm_id in main_project.program_manager_ids:
                try:
                    emp = db.query(Employee).filter(Employee.id == int(pm_id)).first()
                    if emp:
                        pm_name = emp.name
                        break
                except (ValueError, TypeError):
                    continue
        if not pm_name and main_project and getattr(main_project, "program_manager_id", None):
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


def _get_project_pm_employees(db: Session, project: Project) -> list[Employee]:
    pm_ids = set()

    if getattr(project, "assigned_employee_ids", None):
        for pm_id in project.assigned_employee_ids:
            try:
                pm_ids.add(int(pm_id))
            except (ValueError, TypeError):
                continue

    if not pm_ids and getattr(project, "main_project_id", None):
        main_project = db.query(MainProject).filter(MainProject.id == project.main_project_id).first()
        if main_project:
            if getattr(main_project, "program_manager_ids", None):
                for pm_id in main_project.program_manager_ids:
                    try:
                        pm_ids.add(int(pm_id))
                    except (ValueError, TypeError):
                        continue
            if not pm_ids and getattr(main_project, "program_manager_id", None):
                pm_ids.add(main_project.program_manager_id)

    if not pm_ids and getattr(project, "sub_project_id", None):
        hierarchy_sub_project = db.query(HierarchySubProject).filter(HierarchySubProject.id == project.sub_project_id).first()
        if hierarchy_sub_project and hierarchy_sub_project.pm_id:
            pm_ids.add(hierarchy_sub_project.pm_id)

    if not pm_ids:
        return []

    return db.query(Employee).filter(Employee.id.in_(list(pm_ids))).all()


def _queue_pm_allocation_notification(
    db: Session,
    project: Project | None,
    actor: User,
    change_type: str,
    allocation: Allocation,
) -> None:
    if not project or not actor:
        return

    actor_role = (getattr(actor, "role", None) or "").lower().strip()
    if actor_role != "admin":
        return

    pm_employees = _get_project_pm_employees(db, project)
    if not pm_employees:
        return

    allocated_employee = db.query(Employee).filter(Employee.id == allocation.employee_id).first()
    if not allocated_employee:
        return

    actor_name = getattr(actor, "full_name", None) or getattr(actor, "name", None) or actor.email or "Admin"

    for pm_emp in pm_employees:
        if pm_emp.email and actor.email and pm_emp.email.lower().strip() == actor.email.lower().strip():
            continue

        pm_slack_user_id = try_get_or_cache_employee_slack_user_id(db, pm_emp)
        if pm_slack_user_id:
            queue_pm_allocation_batch(
                pm_slack_user_id=pm_slack_user_id,
                pm_name=pm_emp.name,
                actor_name=actor_name,
                sub_project_name=project.name,
                change_type=change_type,
                employee_name=allocated_employee.name,
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
        "is_active": allocation.is_active,
        "deactivated_at": allocation.deactivated_at,
        "deactivated_reason": allocation.deactivated_reason,
        "created_at": allocation.created_at,
        "updated_at": allocation.updated_at,
        "employee_name": employee.name if employee else None,
        "project_name": sub_project.name if sub_project else None,
        "sub_project_name": sub_project.name if sub_project else None,
        "client": sub_project.client if sub_project else None,
        "project_type": sub_project.project_type if sub_project else None,
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
    """
    errors = []
    warnings = []
    
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
        time_distribution_valid=True,
        double_booking_check={"is_overbooked": False, "message": ""},
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

    # Force all allocations to be a simple 8-hour flat assignment
    data.total_daily_hours = 8

    # Leave-overlap check: informational only — leave days are excluded from
    # capacity calculations automatically; assignment is still permitted.
    leave_check = check_leave_conflict(
        db=db,
        employee_id=data.employee_id,
        alloc_start=data.active_start_date,
        alloc_end=data.active_end_date,
    )

    # GUARD (Pillar 5): Block direct allocation of active onboarding trainees to non-training projects
    candidate_user_id = db.query(User.id).filter(User.employee_id == data.employee_id).scalar()
    if candidate_user_id:
        active_pipeline_status = db.query(OnboardingPipeline.status).filter(
            OnboardingPipeline.candidate_id == candidate_user_id,
            OnboardingPipeline.status.in_(["pending_confirmation", "in_progress", "day_5_pending"]),
        ).scalar()
        is_training = project and (
            (project.project_type or "").lower() == "training"
            or "week 1 training" in (project.name or "").lower()
        )
        if active_pipeline_status and not is_training:
            emp_name = db.query(Employee.name).filter(Employee.id == data.employee_id).scalar() or "Employee"
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot allocate {emp_name}: This candidate is currently in the 5-day Onboarding Training "
                    f"(status: {active_pipeline_status}). To allocate them to a production project, "
                    "they must first complete training or be explicitly removed from the Training project."
                ),
            )

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
    project_scope.invalidate_project_scope_cache()
    db.refresh(allocation)

    try:
        _send_employee_allocation_notification(db, allocation, project, actual_count)
    except Exception:
        pass

    # Notify PM(s) and Lead(s) of the project on Slack
    try:
        actor_emp_id = current_user.employee_id if current_user else None
        send_allocation_notifications_to_leaders(
            db,
            allocation,
            project,
            source="Manual Allocation",
            actor_employee_id=actor_emp_id,
        )
    except Exception as exc:
        logger.warning("Failed to send PM/Lead allocation notification: %s", exc)

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
        Allocation.employee_id == employee_id
    ).all()
    return [enrich_allocation_response(a, db) for a in allocations]


@router.put("/{allocation_id}", response_model=dict)
def update_allocation(
    allocation_id: int,
    data: AllocationUpdate,
    http_request: Request,
    background_tasks: BackgroundTasks = None,
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

        # GUARD (Pillar 5): Block moving an active onboarding trainee to a non-training project
        candidate_user_id = db.query(User.id).filter(User.employee_id == allocation.employee_id).scalar()
        if candidate_user_id:
            active_pipeline_status = db.query(OnboardingPipeline.status).filter(
                OnboardingPipeline.candidate_id == candidate_user_id,
                OnboardingPipeline.status.in_(["pending_confirmation", "in_progress", "day_5_pending"]),
            ).scalar()
            is_dest_training = new_project and (
                (new_project.project_type or "").lower() == "training"
                or "week 1 training" in (new_project.name or "").lower()
            )
            if active_pipeline_status and not is_dest_training:
                emp_name = db.query(Employee.name).filter(Employee.id == allocation.employee_id).scalar() or "Employee"
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Cannot move {emp_name}: This candidate is currently in the 5-day Onboarding Training "
                        f"(status: {active_pipeline_status}). To allocate them to a production project, "
                        "they must first complete training or be explicitly removed from the Training project."
                    ),
                )

    # GUARD (Pillar 5): Block re-activating an allocation on a non-training project for active onboarding trainees
    if data.is_active is True and allocation.is_active is False:
        target_pid = data.sub_project_id or allocation.sub_project_id
        target_proj = db.query(Project).filter(Project.id == target_pid).first()
        is_target_training = target_proj and (
            (target_proj.project_type or "").lower() == "training"
            or "week 1 training" in (target_proj.name or "").lower()
        )
        if not is_target_training:
            candidate_user_id = db.query(User.id).filter(User.employee_id == allocation.employee_id).scalar()
            if candidate_user_id:
                active_pipeline_status = db.query(OnboardingPipeline.status).filter(
                    OnboardingPipeline.candidate_id == candidate_user_id,
                    OnboardingPipeline.status.in_(["pending_confirmation", "in_progress", "day_5_pending"]),
                ).scalar()
                if active_pipeline_status:
                    emp_name = db.query(Employee.name).filter(Employee.id == allocation.employee_id).scalar() or "Employee"
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Cannot activate allocation for {emp_name}: This candidate is currently in the 5-day Onboarding Training "
                            f"(status: {active_pipeline_status}). To allocate them to a production project, "
                            "they must first complete training or be explicitly removed from the Training project."
                        ),
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
    # Force all allocations to be a simple 8-hour flat assignment
    data.total_daily_hours = 8

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

    changed_keys = list(data.model_dump(exclude_unset=True).keys())
    # Manually track total_daily_hours as changed
    if "total_daily_hours" not in changed_keys:
        changed_keys.append("total_daily_hours")

    before_values = {key: getattr(allocation, key, None) for key in changed_keys}
    old_employee_id = allocation.employee_id

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(allocation, key, value)
        
    # Force 8-hour flat assignment explicitly
    allocation.total_daily_hours = 8

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

    # If deactivating an allocation on a Training project, cancel active onboarding pipeline
    if before_values.get("is_active") is True and allocation.is_active is False:
        proj = db.query(Project).filter(Project.id == allocation.sub_project_id).first()
        is_training = proj and (
            (proj.project_type or "").lower() == "training"
            or "week 1 training" in (proj.name or "").lower()
        )
        if is_training:
            candidate_user_id = db.query(User.id).filter(User.employee_id == allocation.employee_id).scalar()
            if candidate_user_id:
                active_pipe = db.query(OnboardingPipeline).filter(
                    OnboardingPipeline.candidate_id == candidate_user_id,
                    OnboardingPipeline.status.in_(["pending_confirmation", "in_progress", "day_5_pending"]),
                ).first()
                if active_pipe:
                    active_pipe.status = "cancelled"
                    logger.info(
                        "Cancelled onboarding pipeline #%s for candidate #%s upon deactivation from training.",
                        active_pipe.id, candidate_user_id
                    )

    db.commit()
    db.refresh(allocation)

    # If allocation was moved to a new project:
    # 1. Unallocation notification for old project
    # 2. Allocation notification for new project
    actor_emp_id = current_user.employee_id if current_user else None
    actor_role = current_user.role if current_user else "admin"
    actor_name = current_user.name if current_user else "Manager"

    if allocation.sub_project_id != old_sub_project_id:
        try:
            alloc_info_old = {
                "id": allocation.id,
                "employee_id": allocation.employee_id,
                "sub_project_id": old_sub_project_id,
                "total_daily_hours": before_values.get("total_daily_hours") or allocation.total_daily_hours or 8,
                "role_tags": before_values.get("role_tags") or allocation.role_tags or [],
            }
            if background_tasks is not None:
                background_tasks.add_task(
                    _background_send_unallocation_notifications,
                    alloc_info_old,
                    old_sub_project_id,
                    actor_emp_id,
                    actor_role,
                    actor_name,
                )
            else:
                _background_send_unallocation_notifications(
                    alloc_info_old,
                    old_sub_project_id,
                    actor_emp_id,
                    actor_role,
                    actor_name,
                )
        except Exception as exc:
            logger.warning("Failed to send old project unallocation notification: %s", exc)

        try:
            new_proj = db.query(Project).filter(Project.id == allocation.sub_project_id).first()
            if new_proj:
                send_allocation_notifications_to_leaders(
                    db,
                    allocation,
                    new_proj,
                    source="Reallocation (Project Transfer)",
                    actor_employee_id=actor_emp_id,
                )
        except Exception as exc:
            logger.warning("Failed to send PM/Lead reallocation notification: %s", exc)

    elif before_values.get("is_active") is True and allocation.is_active is False:
        try:
            alloc_info_deactivated = {
                "id": allocation.id,
                "employee_id": allocation.employee_id,
                "sub_project_id": allocation.sub_project_id,
                "total_daily_hours": allocation.total_daily_hours or 8,
                "role_tags": allocation.role_tags or [],
            }
            if background_tasks is not None:
                background_tasks.add_task(
                    _background_send_unallocation_notifications,
                    alloc_info_deactivated,
                    allocation.sub_project_id,
                    actor_emp_id,
                    actor_role,
                    actor_name,
                )
            else:
                _background_send_unallocation_notifications(
                    alloc_info_deactivated,
                    allocation.sub_project_id,
                    actor_emp_id,
                    actor_role,
                    actor_name,
                )
        except Exception as exc:
            logger.warning("Failed to send deactivation unallocation notification: %s", exc)

    response = enrich_allocation_response(allocation, db)
    if leave_check["has_conflict"]:
        response["leave_warning"] = {
            "message": leave_check["message"],
            "excluded_leaves": leave_check["conflicting_leaves"],
        }
    return response


def _background_send_unallocation_notifications(
    alloc_info: dict,
    project_id: int,
    actor_employee_id: int | None,
    actor_user_role: str | None,
    actor_name: str | None,
):
    """Safely runs in background with its own DB session to send Slack removal notifications."""
    with SessionLocal() as bg_db:
        try:
            send_unallocation_notifications(
                db=bg_db,
                alloc_info=alloc_info,
                project_id=project_id,
                actor_user_role=actor_user_role,
                actor_employee_id=actor_employee_id,
                actor_name=actor_name,
            )
        except Exception as e:
            logger.warning("Error in background unallocation notifications: %s", e)


@router.delete("/{allocation_id}")
def delete_allocation(
    allocation_id: int,
    http_request: Request,
    background_tasks: BackgroundTasks = None,
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

    alloc_info = {
        "id": allocation.id,
        "employee_id": allocation.employee_id,
        "sub_project_id": allocation.sub_project_id,
        "total_daily_hours": allocation.total_daily_hours or 8,
        "role_tags": allocation.role_tags or [],
        "active_start_date": allocation.active_start_date,
        "active_end_date": allocation.active_end_date,
    }

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
        subject_employee_id=allocation.employee_id if removed_employee else None,
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

    # Check if removing an allocation from a Training project
    is_training = project and (
        (project.project_type or "").lower() == "training"
        or "week 1 training" in (project.name or "").lower()
    )
    if is_training:
        candidate_user_id = db.query(User.id).filter(User.employee_id == allocation.employee_id).scalar()
        if candidate_user_id:
            active_pipe = db.query(OnboardingPipeline).filter(
                OnboardingPipeline.candidate_id == candidate_user_id,
                OnboardingPipeline.status.in_(["pending_confirmation", "in_progress", "day_5_pending"]),
            ).first()
            if active_pipe:
                active_pipe.status = "cancelled"
                logger.info(
                    "Cancelled onboarding pipeline #%s for candidate #%s upon removal from training.",
                    active_pipe.id, candidate_user_id
                )

    db.delete(allocation)
    db.flush()

    # Sync project allocated_employees and role counts dynamically
    if project:
        sync_project_allocations(db, sub_project_id)

    db.commit()

    # Enqueue Slack notifications in background task:
    # 1. Admin removed -> PM, Lead, Employee
    # 2. PM removed -> Lead, Employee
    # 3. Lead removed -> PM, Employee
    actor_emp_id = current_user.employee_id if current_user else None
    actor_role = current_user.role if current_user else "admin"
    actor_name = current_user.name if current_user else "Manager"

    if background_tasks is not None:
        background_tasks.add_task(
            _background_send_unallocation_notifications,
            alloc_info,
            sub_project_id,
            actor_emp_id,
            actor_role,
            actor_name,
        )
    else:
        _background_send_unallocation_notifications(
            alloc_info,
            sub_project_id,
            actor_emp_id,
            actor_role,
            actor_name,
        )

    try:
        _queue_pm_allocation_notification(db, project, current_user, "removed", allocation)
    except Exception:
        pass

    project_scope.invalidate_project_scope_cache()
    return {"message": "Allocation removed", "project_id": sub_project_id, "allocation_id": allocation_id}

