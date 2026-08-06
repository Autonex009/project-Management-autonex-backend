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
    """required_manpower = Autonex Annotators + Autonex Reviewers.

    QC is no longer part of a project's team composition and is excluded. The column
    remains so historical rows keep their value, but nothing adds to it.

    The project's managers and team leads are deliberately *not* counted here: they are
    added when the ratio is displayed (``totalRequiredManpower`` on the frontend), because
    resolving them needs the allocations and the organisation fallback.
    """
    def g(name):
        if isinstance(source, dict):
            return source.get(name) or 0
        return getattr(source, name, 0) or 0
    return int(g("autonex_annotators")) + int(g("autonex_reviewers"))

router = APIRouter(
    prefix="/api/sub-projects",
    tags=["sub-projects"],
    dependencies=[Depends(get_current_user)],
)

# Mirrors TEAM_LEAD_TAG in frontend/src/utils/roleAccess.js, and the values
# project_scope.TEAM_LEAD_TAGS matches.
TEAM_LEAD_ROLE_TAG = "Team Lead"


def _allocate_project_creator(db: Session, project: Project, current_user: User) -> None:
    """Allocate whoever just created ``project`` to it.

    An admin is skipped: they create projects on other people's behalf and are not staff on
    them, so an allocation would put them in the manpower count and the avatar strip.

    A team lead's row is tagged, which is what records them as this project's lead — it is
    the per-project marker, unlike ``assigned_employee_ids`` which would give them a
    manager's rank over the project's other leads.

    Overridden on purpose: the capacity guard exists to stop someone being booked past their
    working day, and it would refuse anyone who already runs a project. Creating a project
    should never fail for that reason, and the recorded reason keeps it auditable.
    """
    if not current_user or current_user.role == "admin":
        return
    employee_id = getattr(current_user, "employee_id", None)
    if not employee_id:
        return

    already_on = (
        db.query(Allocation)
        .filter(
            Allocation.employee_id == employee_id,
            Allocation.sub_project_id == project.id,
        )
        .first()
    )
    if already_on:
        return

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
            override_reason="Created this project",
        )
    )
    # Denormalised count the cards read; kept in step with the row just added.
    project.allocated_employees = (
        db.query(Allocation).filter(Allocation.sub_project_id == project.id).count() + 1
    )


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

    # Put the creator on their own project straight away. Being the recorded manager is not
    # enough on its own: the Allocations page and the manpower avatar strip are built from
    # allocation rows, so without one the person who just created the project is missing
    # from both. Done here rather than in the client so it cannot be skipped or half-applied.
    _allocate_project_creator(db, project, current_user)

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
    return project


# ✅ LIST PROJECTS
@router.get("", response_model=list[ProjectResponse])
def list_projects(db: Session = Depends(get_db)):
    return db.query(Project).order_by(Project.id.asc()).all()


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

    db.commit()
    db.refresh(project)

    # Note: project edits no longer notify allocated employees. Employees are only
    # notified when they are newly added to a project (handled in the allocations API).

    return project


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
