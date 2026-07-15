from fastapi import APIRouter, Depends, HTTPException
from app.services.auth_service import get_current_user
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.project import SubProject, Project  # SubProject with alias
from app.models.allocation import Allocation
from app.models.employee import Employee
from app.models.parent_project import ParentProject
from app.models.perf_eval import PerfEvaluation, PerfProjectParams
from app.schemas.project import (
    ProjectCreate,
    ProjectUpdate,
    ProjectResponse,
)
from app.services.slack_service import (
    notify_employee_sub_project_updated,
    try_get_or_cache_employee_slack_user_id,
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

router = APIRouter(
    prefix="/api/sub-projects",
    tags=["sub-projects"],
    dependencies=[Depends(get_current_user)],
)


def _format_avg_time_per_task(project: Project) -> str:
    return f"{project.estimated_time_per_task} hr/task"


def _format_target_tasks_per_employee(project: Project) -> str:
    allocated_count = project.allocated_employees or 0
    if allocated_count > 0 and project.total_tasks:
        return str(round(project.total_tasks / allocated_count, 2))
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
    if not getattr(project, "main_project_id", None):
        return "Unassigned"
    parent_project = db.query(ParentProject).filter(ParentProject.id == project.main_project_id).first()
    if not parent_project or not parent_project.program_manager_id:
        return "Unassigned"
    pm = db.query(Employee).filter(Employee.id == parent_project.program_manager_id).first()
    return pm.name if pm else "Unassigned"


def _build_changes_summary(project_before: dict, project_after: Project) -> str:
    tracked_fields = [
        ("name", "Name"),
        ("project_status", "Status"),
        ("daily_target", "Daily Target"),
        ("estimated_time_per_task", "Estimated Time/Task"),
        ("start_date", "Start Date"),
        ("end_date", "End Date"),
        ("required_manpower", "Required Manpower"),
        ("priority", "Priority"),
    ]
    changes = []
    for field_name, label in tracked_fields:
        old_value = project_before.get(field_name)
        new_value = getattr(project_after, field_name, None)
        old_display = old_value.isoformat() if hasattr(old_value, "isoformat") and old_value else old_value
        new_display = new_value.isoformat() if hasattr(new_value, "isoformat") and new_value else new_value
        if old_display != new_display:
            changes.append(f"• {label}: {old_display or 'N/A'} -> {new_display or 'N/A'}")
    return "\n".join(changes) if changes else "Project details were refreshed."


def _notify_allocated_employees_of_project_update(db: Session, project: Project, employee_ids: list[int], changes_summary: str) -> None:
    if not employee_ids:
        return

    employees = db.query(Employee).filter(Employee.id.in_(employee_ids)).all()
    for employee in employees:
        slack_user_id = try_get_or_cache_employee_slack_user_id(db, employee)
        if not slack_user_id:
            continue
        notify_employee_sub_project_updated(
            employee_slack_user_id=slack_user_id,
            employee_name=employee.name,
            sub_project_name=project.name,
            project_manager_name=_get_project_manager_name(db, project),
            avg_time_per_task=_format_avg_time_per_task(project),
            target_tasks_per_employee=_format_target_tasks_per_employee(project),
            timeline=_format_timeline(project),
            status=project.project_status,
            changes_summary=changes_summary,
        )

# ✅ CREATE PROJECT
@router.post("", response_model=ProjectResponse)
def create_project(
    payload: ProjectCreate,
    db: Session = Depends(get_db)
):
    project = Project(**normalize_project_payload(payload.model_dump(), db))
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


# ✅ LIST PROJECTS
@router.get("", response_model=list[ProjectResponse])
def list_projects(db: Session = Depends(get_db)):
    return db.query(Project).order_by(Project.id.asc()).all()


# ✅ UPDATE PROJECT
@router.put("/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: int,
    payload: ProjectUpdate,
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter(Project.id == project_id).first()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    allocated_employee_ids = [
        row[0]
        for row in db.query(Allocation.employee_id).filter(Allocation.sub_project_id == project_id).all()
    ]
    project_before = {
        "name": project.name,
        "project_status": project.project_status,
        "daily_target": project.daily_target,
        "estimated_time_per_task": project.estimated_time_per_task,
        "start_date": project.start_date,
        "end_date": project.end_date,
        "required_manpower": project.required_manpower,
        "priority": project.priority,
    }

    update_data = normalize_project_payload(payload.model_dump(exclude_unset=True), db)
    old_status = project.project_status
    new_status = update_data.get('project_status', old_status)

    for key, value in update_data.items():
        setattr(project, key, value)

    # Auto-release: when project is completed, delete all allocations
    if new_status == 'completed' and old_status != 'completed':
        db.query(Allocation).filter(Allocation.sub_project_id == project_id).delete()
        project.allocated_employees = 0

    db.commit()
    db.refresh(project)

    try:
        changes_summary = _build_changes_summary(project_before, project)
        _notify_allocated_employees_of_project_update(db, project, allocated_employee_ids, changes_summary)
    except Exception:
        pass

    return project


# ✅ DELETE PROJECT
@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter(Project.id == project_id).first()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Delete related allocations + performance evaluations first (FK / orphan cleanup)
    db.query(Allocation).filter(Allocation.sub_project_id == project_id).delete()
    db.query(PerfEvaluation).filter(PerfEvaluation.project_id == project_id).delete()
    db.query(PerfProjectParams).filter(PerfProjectParams.project_id == project_id).delete()

    db.delete(project)
    db.commit()
    return {"message": "Project deleted successfully"}
