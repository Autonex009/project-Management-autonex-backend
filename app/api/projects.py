from fastapi import APIRouter, Depends, HTTPException, Request
from app.services.auth_service import get_current_user, require_role
from app.services import audit_service, project_scope
from sqlalchemy.orm import Session

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
        db.query(Allocation).filter(Allocation.sub_project_id == project.id).count()
    )


def enrich_project_response(db: Session, project: Project) -> dict:
    from datetime import date
    today = date.today()
    allocs = db.query(Allocation).filter(Allocation.sub_project_id == project.id).all()
    
    lead_count = 0
    pm_count = 0
    
    for alloc in allocs:
        # Ignore stale allocations
        if alloc.active_end_date and alloc.active_end_date < today:
            continue
            
        if alloc.role_tags and TEAM_LEAD_ROLE_TAG in alloc.role_tags:
            lead_count += 1
        elif project_scope.escalates_to_admin(db, alloc.employee_id):
            pm_count += 1
            
    resp = ProjectResponse.model_validate(project).model_dump()
    resp["allocated_pm_count"] = pm_count
    resp["allocated_lead_count"] = lead_count
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
            db.query(Allocation).filter(Allocation.sub_project_id == project.id).count()
        )

    # Auto-release: when project is completed, delete all allocations
    released_allocations = 0
    if new_status == 'completed' and old_status != 'completed':
        released_allocations = db.query(Allocation).filter(
            Allocation.sub_project_id == project_id
        ).count()
        db.query(Allocation).filter(Allocation.sub_project_id == project_id).delete()
        project.allocated_employees = 0

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
