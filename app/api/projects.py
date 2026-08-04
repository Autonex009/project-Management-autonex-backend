from fastapi import APIRouter, Depends, HTTPException, Request
from app.services.auth_service import get_current_user, require_role
from app.services import audit_service
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
    """required_manpower = Autonex Annotators + Autonex Reviewers + QC."""
    def g(name):
        if isinstance(source, dict):
            return source.get(name) or 0
        return getattr(source, name, 0) or 0
    return int(g("autonex_annotators")) + int(g("autonex_reviewers")) + int(g("qc_count"))

router = APIRouter(
    prefix="/api/sub-projects",
    tags=["sub-projects"],
    dependencies=[Depends(get_current_user)],
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
    data["required_manpower"] = _autonex_headcount(data)  # auto: Autonex annotators + reviewers + QC
    # A PM (or HR) who creates a project owns it. Guarantee they're recorded as a
    # project-level PM using their authoritative token identity, so the project is
    # always visible to them — even if the client omitted the assignment (e.g. a
    # cached session whose user object had no employee_id). Admin-created projects
    # are left as-is (admins see everything anyway).
    if current_user.role in ("pm", "hr") and current_user.employee_id:
        ids = list(data.get("assigned_employee_ids") or [])
        if current_user.employee_id not in ids:
            ids.insert(0, current_user.employee_id)
        data["assigned_employee_ids"] = ids
    project = Project(**data)
    db.add(project)
    db.flush()

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
