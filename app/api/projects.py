from fastapi import APIRouter, Depends, HTTPException
from app.services.auth_service import get_current_user, require_role
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
    db: Session = Depends(get_db)
):
    data = normalize_project_payload(payload.model_dump(), db)
    data["required_manpower"] = _autonex_headcount(data)  # auto: Autonex annotators + reviewers + QC
    project = Project(**data)
    db.add(project)
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
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter(Project.id == project_id).first()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    update_data = normalize_project_payload(payload.model_dump(exclude_unset=True), db)
    old_status = project.project_status
    new_status = update_data.get('project_status', old_status)

    for key, value in update_data.items():
        setattr(project, key, value)

    # Keep required_manpower in sync with the Autonex headcount
    project.required_manpower = _autonex_headcount(project)

    # Auto-release: when project is completed, delete all allocations
    if new_status == 'completed' and old_status != 'completed':
        db.query(Allocation).filter(Allocation.sub_project_id == project_id).delete()
        project.allocated_employees = 0

    db.commit()
    db.refresh(project)

    # Note: project edits no longer notify allocated employees. Employees are only
    # notified when they are newly added to a project (handled in the allocations API).

    return project


# ✅ DELETE PROJECT
@router.delete("/{project_id}", dependencies=[Depends(require_role("admin", "pm"))])
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
