from fastapi import APIRouter, Depends, HTTPException, Request, Query
from app.services.auth_service import get_current_user, require_role
from app.services import audit_service, project_scope
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.db.database import get_db
from app.models.project import SubProject, Project  # SubProject with alias
from app.models.allocation import Allocation
from app.models.employee import Employee
from app.models.user import User
from app.models.parent_project import ParentProject
from app.models.perf_eval import PerfEvaluation, PerfProjectParams
from app.schemas.project import (
    ProjectCreate,
    ProjectUpdate,
    ProjectResponse,
)



def normalize_project_payload(data: dict, db: Session | None = None) -> dict:
    """Map legacy schema field names to the current DailySheet model."""
    normalized = dict(data)

    if "previous_sub_project_id" in normalized:
        normalized["previous_daily_sheet_id"] = normalized.pop("previous_sub_project_id")

    main_project_id = normalized.get("main_project_id")
    if db and main_project_id:
        parent_project = db.query(ParentProject).filter(ParentProject.id == main_project_id).first()
        if parent_project:
            if not normalized.get("project_type"):
                normalized["project_type"] = parent_project.project_type or "Full"
            if not normalized.get("client"):
                normalized["client"] = parent_project.client or ""

    if not normalized.get("project_type"):
        normalized["project_type"] = "Full"

    return normalized


def _autonex_headcount(source) -> int:
    """Every role the project asks for, summed into ``required_manpower``.

    Annotators + reviewers + others + team leads + team managers + developers.

    Team leads and managers ARE counted here — the client derives those two from the
    people picked in the Team Lead and Program Manager fields rather than accepting a
    typed number, so the headcount cannot contradict the roster on the same screen. That
    also means the displayed ratio uses this figure as-is and must not add them again.

    QC is excluded: it is no longer part of a project's composition. The column remains so
    historical rows keep their value, but nothing adds to it.

    "Total Annotators" is deliberately absent — it counts the vendor's people as well as
    ours, so it is informational rather than a headcount we staff.
    """
    def g(name):
        if isinstance(source, dict):
            return source.get(name) or 0
        return getattr(source, name, 0) or 0
    return sum(
        int(g(field))
        for field in (
            "autonex_annotators",
            "autonex_reviewers",
            "others_count",
            "team_lead_count",
            "team_manager_count",
            # Development projects staff engineers, so they ARE the team: a project
            # asking for 3 of them must read 0/3, not 0/0. Zero everywhere else.
            "developers_count",
        )
    )

router = APIRouter(
    prefix="/api/sub-projects",
    tags=["sub-projects"],
    dependencies=[Depends(get_current_user)],
)

# Mirrors TEAM_LEAD_TAG in frontend/src/utils/roleAccess.js, and the values
# project_scope.TEAM_LEAD_TAGS matches.
TEAM_LEAD_ROLE_TAG = "Team Lead"


def _allocate_project_leaders(db: Session, project: Project, current_user: User) -> None:
    """Allocate all PMs/TLs assigned to the project."""
    # Ensure current user (creator) is in the list if they are PM/HR
    ids_to_allocate = list(project.assigned_employee_ids or [])
    if current_user and current_user.role in ("pm", "hr") and current_user.employee_id:
        if current_user.employee_id not in ids_to_allocate:
            ids_to_allocate.append(current_user.employee_id)
            
    for employee_id in ids_to_allocate:
        try:
            employee_id = int(employee_id)
        except:
            continue
            
        already_on = (
            db.query(Allocation)
            .filter(
                Allocation.employee_id == employee_id,
                Allocation.sub_project_id == project.id,
            )
            .first()
        )
        if already_on:
            continue

        is_lead = project_scope.escalates_to_pm(db, employee_id)
        db.add(
            Allocation(
                employee_id=employee_id,
                sub_project_id=project.id,
                total_daily_hours=8,
                role_tags=[TEAM_LEAD_ROLE_TAG] if is_lead else [],
                time_distribution={},
                active_start_date=project.start_date,
                active_end_date=project.end_date,
                override_flag=True,
                override_reason="Auto-sync from project manager/lead assignment",
            )
        )
        
    db.flush()
    project.allocated_employees = (
        db.query(Allocation).filter(
            Allocation.sub_project_id == project.id,
            Allocation.is_active == True
        ).count()
    )


def enrich_project_response(db: Session, project: Project) -> dict:
    from datetime import date, timedelta
    import math
    today = date.today()
    allocs = db.query(Allocation).filter(
        Allocation.sub_project_id == project.id,
        Allocation.is_active == True
    ).all()
    
    lead_count = 0
    pm_count = 0
    pm_ids = []
    team_lead_ids = []
    allocated_employee_ids = []
    
    for alloc in allocs:
        # Ignore stale allocations
        if alloc.active_end_date and alloc.active_end_date < today:
            continue
        allocated_employee_ids.append(alloc.employee_id)
            
    # Bulk fetch designations to avoid N+1 escalates_to_admin queries
    from app.models.employee import Employee
    emp_designations = {}
    if allocated_employee_ids:
        emps_query = db.query(Employee.id, Employee.designation).filter(Employee.id.in_(allocated_employee_ids)).all()
        emp_designations = {e.id: str(e.designation or "").lower().strip() for e in emps_query}
        
    for alloc in allocs:
        if alloc.active_end_date and alloc.active_end_date < today:
            continue
            
        if alloc.role_tags and TEAM_LEAD_ROLE_TAG in alloc.role_tags:
            lead_count += 1
            team_lead_ids.append(alloc.employee_id)
        elif emp_designations.get(alloc.employee_id) in ("program manager", "project manager", "hr"):
            pm_count += 1
            pm_ids.append(alloc.employee_id)
            
    # Calculate capacity
    capacity = {"status": "unknown", "recommendation": None}
    if project.end_date:
        task_count = project.remaining_tasks if project.remaining_tasks is not None else project.total_tasks
        required_hours = float(task_count or 0) * float(project.estimated_time_per_task or 0)
        
        # Working days remaining
        def get_working_days(start_dt, end_dt):
            if start_dt > end_dt: return 0
            days = 0
            curr = start_dt
            while curr <= end_dt:
                if curr.weekday() < 5: days += 1
                curr += timedelta(days=1)
            return days
            
        working_days = get_working_days(today, project.end_date)
        
        if working_days <= 0:
            capacity = {"status": "overdue", "recommendation": {"message": "Past deadline"}}
        else:
            active_allocated_count = 0
            if allocated_employee_ids:
                from app.models.leave import Leave
                leaves = db.query(Leave).filter(
                    Leave.employee_id.in_(allocated_employee_ids),
                    Leave.start_date <= project.end_date,
                    Leave.end_date >= project.start_date,
                    Leave.status == "approved"
                ).all()
                leave_emp_ids = {l.employee_id for l in leaves}
                active_allocated_count = len([e for e in allocated_employee_ids if e not in leave_emp_ids])
            
            if project.required_manpower and active_allocated_count >= project.required_manpower:
                capacity = {"status": "balanced", "recommendation": None}
            elif active_allocated_count == 0:
                capacity = {"status": "no_staff", "recommendation": {"message": "Needs staffing"}}
            else:
                standard_day_hours = 8
                total_cap = active_allocated_count * standard_day_hours * working_days
                load_ratio = required_hours / total_cap if total_cap > 0 else float('inf')
                
                if load_ratio > 1.1:
                    if project.required_manpower and active_allocated_count < project.required_manpower:
                        extra = project.required_manpower - active_allocated_count
                        capacity = {"status": "overburden", "recommendation": {"message": f"+{extra} staff needed"}}
                    else:
                        deficit = required_hours - total_cap
                        extra = math.ceil(deficit / (working_days * standard_day_hours))
                        capacity = {"status": "overburden", "recommendation": {"message": f"+{extra} staff needed"}}
                else:
                    capacity = {"status": "balanced", "recommendation": None}
                    
    resp = ProjectResponse.model_validate(project).model_dump()
    resp["allocated_pm_count"] = pm_count
    resp["allocated_lead_count"] = lead_count
    resp["capacity"] = capacity

    # Resolve names
    from app.models.employee import Employee
    all_needed_ids = set(pm_ids + team_lead_ids)
    if all_needed_ids:
        emps = db.query(Employee).filter(Employee.id.in_(all_needed_ids)).all()
        emp_map = {e.id: (e.name or "").strip() for e in emps}
        resp["pm_names"] = [emp_map.get(pid, "Unknown") for pid in pm_ids]
        resp["team_lead_names"] = [emp_map.get(lid, "Unknown") for lid in team_lead_ids]
        resp["pm_ids"] = pm_ids
        resp["team_lead_ids"] = team_lead_ids
    else:
        resp["pm_names"] = []
        resp["team_lead_names"] = []
        resp["pm_ids"] = []
        resp["team_lead_ids"] = []

    return resp


# ✅ CREATE PROJECT
@router.post("", response_model=ProjectResponse, dependencies=[Depends(require_role("admin", "pm"))])
def create_project(
    payload: ProjectCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    data = normalize_project_payload(payload.model_dump(), db)
    data["required_manpower"] = _autonex_headcount(data)  # Autonex annotators + reviewers
    # A PM (or HR) who creates a project owns it. Guarantee they're recorded as a
    # project-level PM using their authoritative token identity, so the project is
    # always visible to them — even if the client omitted the assignment (e.g. a
    # cached session whose user object had no employee_id). Admin-created projects
    # are left as-is (admins see everything anyway).
    #

    # A team lead is deliberately NOT added here: a seat in assigned_employee_ids is the
    # manager's, and holding it would let them decide the project's other leads' requests.
    # They are put on the project as its lead instead — see the allocation below.
    if current_user.role in ("pm", "hr") and current_user.employee_id:
        ids = list(data.get("assigned_employee_ids") or [])
        if current_user.employee_id not in ids:
            ids.insert(0, current_user.employee_id)
        data["assigned_employee_ids"] = ids
    project = Project(**data)
    db.add(project)
    db.flush()
    # Put the assigned leaders on the project straight away.
    _allocate_project_leaders(db, project, current_user)

    # A team lead who creates a project owns it from the ground up, so they are
    # allocated to it automatically. They are deliberately NOT added to the project's
    # assigned_employee_ids because that grants PM-level access across the project;
    # they receive an allocation with the Team Lead tag instead.
    if current_user.role == "team_lead" and current_user.employee_id:
        db.add(
            Allocation(
                employee_id=current_user.employee_id,
                sub_project_id=project.id,
                total_daily_hours=8,
                role_tags=[TEAM_LEAD_ROLE_TAG],
                time_distribution={},
                active_start_date=project.start_date,
                active_end_date=project.end_date,
                override_flag=True,
                override_reason="Auto-sync from project creator",
            )
        )
        db.flush()
        project.allocated_employees = (
            db.query(Allocation).filter(
                Allocation.sub_project_id == project.id,
                Allocation.is_active == True
            ).count()
        )

    audit_service.record(
        db,
        actor=current_user,
        action="project.created",
        category="Projects",
        action_type="Created",
        entity_type="daily_sheet",
        entity_id=project.id,
        entity_name=project.name,
        details=audit_service.changes(
            audit_service.field_diff("Client", None, project.client),
            audit_service.field_diff("Project type", None, project.project_type),
            audit_service.field_diff("Status", None, project.project_status),
            audit_service.field_diff("Required manpower", None, project.required_manpower),
            audit_service.field_diff(
                "Assigned PMs", None,
                len(project.assigned_employee_ids or []) or None,
            ),
        ),
        summary=f"Created project {project.name}" + (f" for {project.client}" if project.client else ""),
        request=http_request,
    )

    db.commit()
    db.refresh(project)
    return enrich_project_response(db, project)


# ✅ LIST PROJECTS
@router.get("", response_model=list[dict])
def list_projects(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    projects = db.query(Project).order_by(Project.id.asc()).all()
    
    # Filter projects based on user scope
    if current_user.role in ("pm", "hr", "team_lead") and not project_scope.has_full_access(current_user):
        projects = [p for p in projects if project_scope.can_act_on_project(db, current_user, p)]
        
    return [enrich_project_response(db, p) for p in projects]

# ✅ LIST PROJECTS (PAGINATED)
def bulk_compute_capacity(db: Session, projects: list[Project]) -> dict[int, str]:
    from datetime import date, timedelta
    import math
    from app.models.allocation import Allocation
    from app.models.leave import Leave
    today = date.today()
    if not projects: return {}
    pids = [p.id for p in projects]
    allocs = db.query(Allocation).filter(
        Allocation.sub_project_id.in_(pids),
        Allocation.is_active == True
    ).all()
    emp_ids = {a.employee_id for a in allocs if a.employee_id}
    all_leaves = db.query(Leave).filter(
        Leave.employee_id.in_(emp_ids),
        Leave.start_date <= max([p.end_date for p in projects if p.end_date] or [today]),
        Leave.end_date >= min([p.start_date for p in projects if p.start_date] or [today]),
        Leave.status == "approved"
    ).all() if emp_ids else []
    
    res = {}
    for p in projects:
        cap = "unknown"
        if p.end_date:
            p_allocs = [a for a in allocs if a.sub_project_id == p.id]
            p_emp_ids = [a.employee_id for a in p_allocs if not (a.active_end_date and a.active_end_date < today)]
            
            tc = p.remaining_tasks if p.remaining_tasks is not None else p.total_tasks
            req_hrs = float(tc or 0) * float(p.estimated_time_per_task or 0)
            
            days = 0
            curr = today
            while curr <= p.end_date:
                if curr.weekday() < 5: days += 1
                curr += timedelta(days=1)
            
            if days <= 0: cap = "overdue"
            else:
                p_leaves = [l for l in all_leaves if l.employee_id in p_emp_ids and l.start_date <= p.end_date and l.end_date >= p.start_date]
                leave_eids = {l.employee_id for l in p_leaves}
                active_count = len([e for e in p_emp_ids if e not in leave_eids])
                
                if p.required_manpower and active_count >= p.required_manpower:
                    cap = "balanced"
                elif active_count == 0:
                    cap = "no_staff"
                else:
                    total_cap = active_count * 8 * days
                    lr = req_hrs / total_cap if total_cap > 0 else float('inf')
                    cap = "overburden" if lr > 1.1 else "balanced"
        res[p.id] = cap
    return res

def _get_filtered_enriched_projects(
    db: Session,
    current_user: User,
    search: str | None = None,
    project_view: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    organization: str | None = None,
    autonex_only: bool = False,
    pm_id: int | None = None,
    team_lead_id: int | None = None,
    recommendation: str | None = None,
    main_project_id: int | None = None
):
    query = db.query(Project).order_by(Project.id.asc())
    
    if search:
        query = query.filter(Project.name.ilike(f"%{search}%"))
    if priority and priority != "all":
        query = query.filter(Project.priority == priority)
    if main_project_id:
        query = query.filter(Project.main_project_id == main_project_id)
        
    all_projects = query.all()
    
    if current_user.role in ("pm", "hr", "team_lead") and not project_scope.has_full_access(current_user):
        allowed_ids = project_scope.managed_projects_of_employee(db, current_user, current_user.employee_id)
        all_projects = [p for p in all_projects if p.id in allowed_ids]
        
    tl_valid_pids = set()
    if team_lead_id and team_lead_id != "all":
        from app.models.allocation import Allocation
        tl_allocs = db.query(Allocation.sub_project_id, Allocation.role_tags).filter(
            Allocation.employee_id == team_lead_id,
            Allocation.is_active == True
        ).all()
        tl_valid_pids = {r[0] for r in tl_allocs if r[1] and TEAM_LEAD_ROLE_TAG in r[1]}
        
    filtered = []
    
    for p in all_projects:
        # Client / Organization
        if organization and organization != "all":
            if (p.client or "") != organization:
                continue
                
        # Autonex only
        if autonex_only:
            ann = getattr(p, "autonex_annotators", 0) or 0
            rev = getattr(p, "autonex_reviewers", 0) or 0
            if (ann + rev) <= 0:
                continue
                
        # Status and project_view
        status_val = (p.project_status or "active").lower().strip()
        archived_statuses = ["completed", "on-hold", "cancelled"]
        is_archived = status_val in archived_statuses
        
        types = p.project_types or {}
        is_dev = bool(types and "Development" in types)
        
        if project_view == "development":
            if not is_dev: continue
        elif project_view == "archived":
            if is_dev or not is_archived: continue
        elif project_view in ("active", "active_projects"):
            if is_dev or is_archived: continue
            
        if status and status != "all":
            if status == "active":
                if status_val not in ("active", "in-progress", "in progress"):
                    continue
            elif status == "poc":
                if status_val != "poc": continue
            else:
                if status_val != status.lower(): continue

        if pm_id and pm_id != "all":
            assigned = p.assigned_employee_ids or []
            if str(pm_id) not in [str(x) for x in assigned]:
                continue
                
        if team_lead_id and team_lead_id != "all":
            if p.id not in tl_valid_pids:
                continue
                
        filtered.append(p)
        
    if recommendation and recommendation != "all":
        caps = bulk_compute_capacity(db, filtered)
        filtered = [p for p in filtered if caps.get(p.id, "").lower() == recommendation.lower()]
        
    return filtered


@router.get("/paginated", response_model=dict)
def list_projects_paginated(
    is_dashboard: bool = Query(False),
    page: int = Query(1, ge=1),
    limit: int = Query(5, ge=1, le=100),
    search: str | None = None,
    project_view: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    organization: str | None = None,
    autonex_only: bool = False,
    pm_id: int | None = None,
    team_lead_id: int | None = None,
    recommendation: str | None = None,
    main_project_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    filtered = _get_filtered_enriched_projects(
        db, current_user, search, project_view, status, priority, 
        organization, autonex_only, pm_id, team_lead_id, recommendation, main_project_id
    )
    
    total = len(filtered)
    paginated_projects = filtered[(page - 1) * limit : page * limit]
    
    # Enrich ONLY the visible projects for full payload response

    if is_dashboard:
        items = [{
            "id": p.id,
            "name": p.name,
            "client": p.client,
            "sentiment": p.sentiment,
            "encord_project_hash": p.encord_project_hash,
            "autonex_annotators": p.autonex_annotators,
            "autonex_reviewers": p.autonex_reviewers
        } for p in paginated_projects]
    else:
        items = [enrich_project_response(db, p) for p in paginated_projects]

    
    return {
        "items": items,
        "total": total,
        "page": page,
        "limit": limit
    }

# ✅ PROJECT KPI
@router.get("/kpi", response_model=dict)
def get_projects_kpi(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    filtered = _get_filtered_enriched_projects(db, current_user)
    
    metrics = {
        "totalProjects": len(filtered),
        "activeProjects": 0,
        "overburdenedProjects": 0,
        "balancedProjects": 0,
        "onHoldProjects": 0,
        "completedProjects": 0,
        "cancelledProjects": 0
    }
    
    tab_counts = {
        "active": 0,
        "archived": 0,
        "development": 0
    }
    
    active_for_cap = []
    
    for p in filtered:
        status_val = (p.project_status or "active").lower().strip()
        archived_statuses = ["completed", "on-hold", "cancelled"]
        is_archived = status_val in archived_statuses
        
        types = p.project_types or {}
        is_dev = bool(types and "Development" in types)
        
        # Tab counts
        if is_dev:
            tab_counts["development"] += 1
        elif is_archived:
            tab_counts["archived"] += 1
        else:
            tab_counts["active"] += 1
            
        # Metrics
        if not is_archived and not is_dev:
            metrics["activeProjects"] += 1
            active_for_cap.append(p)
                
        if status_val == "completed":
            metrics["completedProjects"] += 1
        elif status_val == "on-hold":
            metrics["onHoldProjects"] += 1
        elif status_val == "cancelled":
            metrics["cancelledProjects"] += 1
            
    caps = bulk_compute_capacity(db, active_for_cap)
    for p in active_for_cap:
        cap = caps.get(p.id, "")
        if cap == "overburden":
            metrics["overburdenedProjects"] += 1
        elif cap == "balanced":
            metrics["balancedProjects"] += 1
            
    return {
        "metrics": metrics,
        "tab_counts": tab_counts
    }


# ✅ UPDATE PROJECT
@router.put("/{project_id}", response_model=ProjectResponse, dependencies=[Depends(require_role("admin", "pm"))])
def update_project(
    project_id: int,
    payload: ProjectUpdate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = db.query(Project).filter(Project.id == project_id).first()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_scope.require_project_scope(
        db, current_user, project, action="edit this project"
    )

    update_data = normalize_project_payload(payload.model_dump(exclude_unset=True), db)
    old_status = project.project_status
    new_status = update_data.get('project_status', old_status)

    if old_status != "archived" and new_status == "archived":
        db.query(Allocation).filter(
            Allocation.sub_project_id == project.id,
            Allocation.is_active == True
        ).update({
            "is_active": False,
            "deactivated_at": func.now(),
            "deactivated_reason": "project_archived"
        }, synchronize_session=False)
    elif old_status == "archived" and new_status != "archived":
        db.query(Allocation).filter(
            Allocation.sub_project_id == project.id,
            Allocation.is_active == False,
            Allocation.deactivated_reason == "project_archived"
        ).update({
            "is_active": True,
            "deactivated_at": None,
            "deactivated_reason": None
        }, synchronize_session=False)

    # A PM (or HR) editing a project can't drop themselves off it — keep them among
    # the project PMs so they never lose visibility of their own project.
    if (
        "assigned_employee_ids" in update_data
        and current_user.role in ("pm", "hr")
        and current_user.employee_id
    ):
        ids = list(update_data.get("assigned_employee_ids") or [])
        if current_user.employee_id not in ids:
            ids.insert(0, current_user.employee_id)
        update_data["assigned_employee_ids"] = ids

    before = audit_service.snapshot(project, update_data.keys())

    for key, value in update_data.items():
        setattr(project, key, value)

    # Keep required_manpower in sync with the Autonex headcount
    project.required_manpower = _autonex_headcount(project)

    # Auto-allocate any newly assigned PM/TL
    if "assigned_employee_ids" in update_data:
        for employee_id in project.assigned_employee_ids or []:
            try:
                employee_id = int(employee_id)
            except:
                continue
                
            already_on = (
                db.query(Allocation)
                .filter(
                    Allocation.employee_id == employee_id,
                    Allocation.sub_project_id == project.id,
                )
                .first()
            )
            if not already_on:
                is_lead = project_scope.escalates_to_pm(db, employee_id)
                db.add(
                    Allocation(
                        employee_id=employee_id,
                        sub_project_id=project.id,
                        total_daily_hours=8,
                        role_tags=[TEAM_LEAD_ROLE_TAG] if is_lead else [],
                        time_distribution={},
                        active_start_date=project.start_date,
                        active_end_date=project.end_date,
                        override_flag=True,
                        override_reason="Auto-sync from project manager/lead assignment",
                    )
                )
        db.flush()
        project.allocated_employees = (
            db.query(Allocation).filter(
                Allocation.sub_project_id == project.id,
                Allocation.is_active == True
            ).count()
        )

    # Auto-release: when project is completed, delete all allocations
    released_allocations = 0
    if new_status == 'completed' and old_status != 'completed':
        released_allocations = db.query(Allocation).filter(
            Allocation.sub_project_id == project_id
        ).count()
        db.query(Allocation).filter(Allocation.sub_project_id == project_id).delete()
        project.allocated_employees = 0
    elif (old_status != "archived" and new_status == "archived") or (old_status == "archived" and new_status != "archived"):
        project.allocated_employees = (
            db.query(Allocation).filter(
                Allocation.sub_project_id == project.id,
                Allocation.is_active == True
            ).count()
        )

    details = audit_service.diff_all(
        before,
        project,
        {
            "name": "Name",
            "client": "Client",
            "project_status": "Status",
            "project_type": "Project type",
            "assigned_employee_ids": "Assigned PMs",
            "required_manpower": "Required manpower",
            "start_date": "Start date",
            "end_date": "End date",
        },
    )
    # Completing a project silently unassigns everyone on it. That side effect is far
    # more consequential than the status field itself, so it gets its own line.
    if released_allocations:
        details += audit_service.changes(
            audit_service.field_diff(
                "Allocations released", released_allocations, 0
            )
        )

    if details:
        audit_service.record(
            db,
            actor=current_user,
            action="project.updated",
            category="Projects",
            action_type="Updated",
            entity_type="daily_sheet",
            entity_id=project.id,
            entity_name=project.name,
            details=details,
            summary=(
                f"Updated project {project.name} — "
                + ", ".join(d["field"] for d in details[:4])
                + (
                    f"; completing it released {released_allocations} allocation"
                    f"{'s' if released_allocations != 1 else ''}"
                    if released_allocations
                    else ""
                )
            ),
            request=http_request,
        )

    # Also sync new allocations if new leaders were added
    _allocate_project_leaders(db, project, current_user)
    
    db.commit()
    db.refresh(project)

    # Note: project edits no longer notify allocated employees. Employees are only
    # notified when they are newly added to a project (handled in the allocations API).

    return enrich_project_response(db, project)


# ✅ DELETE PROJECT
@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    project = db.query(Project).filter(Project.id == project_id).first()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_scope.require_project_scope(
        db, current_user, project, action="delete this project"
    )

    # Counted before deletion — this endpoint quietly destroys allocations and
    # performance history alongside the project, and the entry should say how much.
    lost_allocations = db.query(Allocation).filter(Allocation.sub_project_id == project_id).count()
    lost_evals = db.query(PerfEvaluation).filter(PerfEvaluation.project_id == project_id).count()

    audit_service.record(
        db,
        actor=current_user,
        action="project.deleted",
        category="Projects",
        action_type="Deleted",
        entity_type="daily_sheet",
        entity_id=project.id,
        entity_name=project.name,
        details=audit_service.changes(
            audit_service.field_diff("Client", project.client, None),
            audit_service.field_diff("Status at deletion", project.project_status, None),
            audit_service.field_diff("Allocations destroyed", lost_allocations or None, None),
            audit_service.field_diff("Performance evaluations destroyed", lost_evals or None, None),
        ),
        summary=(
            f"Deleted project {project.name}"
            + (
                f" — also destroyed {lost_allocations} allocation"
                f"{'s' if lost_allocations != 1 else ''}"
                if lost_allocations
                else ""
            )
            + (
                f" and {lost_evals} performance evaluation"
                f"{'s' if lost_evals != 1 else ''}"
                if lost_evals
                else ""
            )
        ),
        request=http_request,
    )

    # Delete related allocations + performance evaluations first (FK / orphan cleanup)
    db.query(Allocation).filter(Allocation.sub_project_id == project_id).delete()
    db.query(PerfEvaluation).filter(PerfEvaluation.project_id == project_id).delete()
    db.query(PerfProjectParams).filter(PerfProjectParams.project_id == project_id).delete()

    db.delete(project)
    db.commit()
    return {"message": "Project deleted successfully"}
