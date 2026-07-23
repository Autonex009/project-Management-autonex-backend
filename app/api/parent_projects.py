from fastapi import APIRouter, Depends, HTTPException, status
from app.services.auth_service import get_current_user, require_role
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List

from app.db.database import get_db
from app.models.allocation import Allocation
from app.models.parent_project import MainProject, ParentProject
from app.models.project import SubProject, Project
from app.models.sub_project import SubProject as SubProjectGroup
from app.models.perf_eval import PerfEvaluation, PerfProjectParams
from app.models.employee import Employee
from app.schemas.parent_project import (
    ParentProjectCreate,
    ParentProjectUpdate,
    ParentProjectResponse,
    ParentProjectWithSubProjects,
    SubProjectSummary
)

router = APIRouter(prefix="/api/projects", tags=["projects"], dependencies=[Depends(get_current_user)])


def get_pm_name(db: Session, pm_id: int) -> str | None:
    """Helper to fetch program manager name."""
    if not pm_id:
        return None
    employee = db.query(Employee).filter(Employee.id == pm_id).first()
    return employee.name if employee else None


def get_pm_ids(pp) -> list[int]:
    """All PM ids for a project (multi-PM list, falling back to single column)."""
    ids = pp.program_manager_ids or []
    if not ids and pp.program_manager_id:
        ids = [pp.program_manager_id]
    return ids


def get_pm_names(db: Session, pm_ids: list[int]) -> list[str]:
    """Names for a list of PM ids, preserving order."""
    if not pm_ids:
        return []
    employees = db.query(Employee).filter(Employee.id.in_(pm_ids)).all()
    name_map = {e.id: e.name for e in employees}
    return [name_map[i] for i in pm_ids if i in name_map]


def normalize_pm_fields(db: Session, data: dict) -> dict:
    """Merge program_manager_id / program_manager_ids into a validated, consistent pair.
    The first valid PM in the list becomes the primary program_manager_id."""
    ids = data.get("program_manager_ids") or []
    if not ids and data.get("program_manager_id"):
        ids = [data["program_manager_id"]]

    # De-duplicate preserving order, keep only existing employees
    seen = set()
    ordered = [i for i in ids if not (i in seen or seen.add(i))]
    if ordered:
        valid = {row[0] for row in db.query(Employee.id).filter(Employee.id.in_(ordered)).all()}
        ordered = [i for i in ordered if i in valid]

    data["program_manager_ids"] = ordered
    data["program_manager_id"] = ordered[0] if ordered else None
    return data


def release_project_allocations(db: Session, parent_project_id: int) -> None:
    """Release all allocations belonging to sub-projects under a main project."""
    sub_project_ids = [
        row[0]
        for row in db.query(Project.id).filter(Project.main_project_id == parent_project_id).all()
    ]

    if not sub_project_ids:
        return

    db.query(Allocation).filter(Allocation.sub_project_id.in_(sub_project_ids)).delete(
        synchronize_session=False
    )
    db.query(Project).filter(Project.id.in_(sub_project_ids)).update(
        {"allocated_employees": 0},
        synchronize_session=False,
    )


@router.get("", response_model=List[ParentProjectResponse])
def get_all_parent_projects(db: Session = Depends(get_db)):
    """Get all parent projects with sub-project counts (optimized)."""
    parent_projects = db.query(ParentProject).order_by(ParentProject.created_at.desc()).all()
    
    if not parent_projects:
        return []

    # Batch load sub-project counts in a single query
    sub_counts = db.query(
        Project.main_project_id, 
        func.count(Project.id).label('count')
    ).group_by(Project.main_project_id).all()
    sub_count_map = {row[0]: row[1] for row in sub_counts}
    
    # Batch load all PMs (primary + multi) in a single query
    pm_ids = set()
    for pp in parent_projects:
        pm_ids.update(get_pm_ids(pp))
    if pm_ids:
        pms = db.query(Employee).filter(Employee.id.in_(pm_ids)).all()
        pm_map = {pm.id: pm.name for pm in pms}
    else:
        pm_map = {}
    
    result = []
    for pp in parent_projects:
        all_pm_ids = get_pm_ids(pp)
        response = ParentProjectResponse(
            id=pp.id,
            name=pp.name,
            program_manager_id=pp.program_manager_id,
            program_manager_ids=all_pm_ids,
            description=pp.description,
            client=pp.client,
            project_type=pp.project_type,
            is_annotation=pp.is_annotation,
            global_start_date=pp.global_start_date,
            tentative_duration_months=pp.tentative_duration_months,
            status=pp.status,
            created_at=pp.created_at,
            updated_at=pp.updated_at,
            sub_projects_count=sub_count_map.get(pp.id, 0),
            program_manager_name=pm_map.get(pp.program_manager_id) if pp.program_manager_id else None,
            program_manager_names=[pm_map[i] for i in all_pm_ids if i in pm_map]
        )
        result.append(response)
    
    return result


@router.post("", response_model=ParentProjectResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_role("admin", "pm"))])
def create_parent_project(
    parent_project: ParentProjectCreate,
    db: Session = Depends(get_db)
):
    """Create a new organization. Only the name is required; a PM is optional
    (attached automatically when a PM creates it)."""
    # Normalize PM fields (supports multiple PMs). PMs are optional here.
    data = normalize_pm_fields(db, parent_project.model_dump())

    db_parent_project = ParentProject(**data)
    db.add(db_parent_project)
    db.commit()
    db.refresh(db_parent_project)
    
    all_pm_ids = get_pm_ids(db_parent_project)
    return ParentProjectResponse(
        id=db_parent_project.id,
        name=db_parent_project.name,
        program_manager_id=db_parent_project.program_manager_id,
        program_manager_ids=all_pm_ids,
        description=db_parent_project.description,
        client=db_parent_project.client,
        project_type=db_parent_project.project_type,
        is_annotation=db_parent_project.is_annotation,
        global_start_date=db_parent_project.global_start_date,
        tentative_duration_months=db_parent_project.tentative_duration_months,
        status=db_parent_project.status,
        created_at=db_parent_project.created_at,
        updated_at=db_parent_project.updated_at,
        sub_projects_count=0,
        program_manager_name=get_pm_name(db, db_parent_project.program_manager_id),
        program_manager_names=get_pm_names(db, all_pm_ids)
    )


@router.get("/{parent_project_id}", response_model=ParentProjectWithSubProjects)
def get_parent_project(parent_project_id: int, db: Session = Depends(get_db)):
    """Get a parent project with its sub-projects."""
    pp = db.query(ParentProject).filter(ParentProject.id == parent_project_id).first()
    
    if not pp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Parent project with ID {parent_project_id} not found"
        )
    
    # Get sub-projects
    sub_projects = db.query(Project).filter(
        Project.main_project_id == parent_project_id
    ).order_by(Project.id.asc()).all()
    
    sub_project_list = [
        SubProjectSummary(
            id=sp.id,
            name=sp.name,
            batch_name=sp.batch_name,
            project_status=sp.project_status
        ) for sp in sub_projects
    ]
    
    return ParentProjectWithSubProjects(
        id=pp.id,
        name=pp.name,
        program_manager_id=pp.program_manager_id,
        program_manager_ids=get_pm_ids(pp),
        description=pp.description,
        client=pp.client,
        project_type=pp.project_type,
        is_annotation=pp.is_annotation,
        global_start_date=pp.global_start_date,
        tentative_duration_months=pp.tentative_duration_months,
        status=pp.status,
        created_at=pp.created_at,
        updated_at=pp.updated_at,
        sub_projects_count=len(sub_project_list),
        program_manager_name=get_pm_name(db, pp.program_manager_id),
        program_manager_names=get_pm_names(db, get_pm_ids(pp)),
        sub_projects=sub_project_list
    )


@router.put("/{parent_project_id}", response_model=ParentProjectResponse, dependencies=[Depends(require_role("admin", "pm"))])
def update_parent_project(
    parent_project_id: int,
    update_data: ParentProjectUpdate,
    db: Session = Depends(get_db)
):
    """Update a parent project."""
    pp = db.query(ParentProject).filter(ParentProject.id == parent_project_id).first()
    
    if not pp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Parent project with ID {parent_project_id} not found"
        )
    
    # Update only provided fields
    update_dict = update_data.model_dump(exclude_unset=True)

    # Normalize PM fields if either was provided
    if "program_manager_id" in update_dict or "program_manager_ids" in update_dict:
        pm_data = normalize_pm_fields(db, {
            "program_manager_id": update_dict.get("program_manager_id"),
            "program_manager_ids": update_dict.get("program_manager_ids"),
        })
        update_dict["program_manager_id"] = pm_data["program_manager_id"]
        update_dict["program_manager_ids"] = pm_data["program_manager_ids"]

    previous_project_type = pp.project_type
    for key, value in update_dict.items():
        setattr(pp, key, value)

    if update_dict.get("project_type") == "POC Rejected" and previous_project_type != "POC Rejected":
        release_project_allocations(db, pp.id)
    
    db.commit()
    db.refresh(pp)
    
    sub_count = db.query(func.count(Project.id)).filter(
        Project.main_project_id == pp.id
    ).scalar()
    
    return ParentProjectResponse(
        id=pp.id,
        name=pp.name,
        program_manager_id=pp.program_manager_id,
        program_manager_ids=get_pm_ids(pp),
        description=pp.description,
        client=pp.client,
        project_type=pp.project_type,
        is_annotation=pp.is_annotation,
        global_start_date=pp.global_start_date,
        tentative_duration_months=pp.tentative_duration_months,
        status=pp.status,
        created_at=pp.created_at,
        updated_at=pp.updated_at,
        sub_projects_count=sub_count,
        program_manager_name=get_pm_name(db, pp.program_manager_id),
        program_manager_names=get_pm_names(db, get_pm_ids(pp))
    )


@router.delete("/{parent_project_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_role("admin", "pm"))])
def delete_parent_project(parent_project_id: int, db: Session = Depends(get_db)):
    """
    Delete a project and everything under it:
    its sub-project groups and daily-sheets, plus the daily-sheets' allocations
    and performance evaluations. Fully removes the project everywhere.
    """
    pp = db.query(ParentProject).filter(ParentProject.id == parent_project_id).first()

    if not pp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Parent project with ID {parent_project_id} not found"
        )

    # Project groups (intermediate level) under this project.
    group_ids = [
        g.id for g in db.query(SubProjectGroup)
        .filter(SubProjectGroup.main_project_id == parent_project_id).all()
    ]

    # Daily-sheets linked directly to the project or via one of its groups.
    sheet_filter = [Project.main_project_id == parent_project_id]
    if group_ids:
        sheet_filter.append(Project.sub_project_id.in_(group_ids))
    sheet_ids = [
        d.id for d in db.query(Project).filter(or_(*sheet_filter)).all()
    ]

    if sheet_ids:
        db.query(Allocation).filter(Allocation.sub_project_id.in_(sheet_ids)).delete(synchronize_session=False)
        db.query(PerfEvaluation).filter(PerfEvaluation.project_id.in_(sheet_ids)).delete(synchronize_session=False)
        db.query(PerfProjectParams).filter(PerfProjectParams.project_id.in_(sheet_ids)).delete(synchronize_session=False)
        db.query(Project).filter(Project.id.in_(sheet_ids)).delete(synchronize_session=False)

    if group_ids:
        db.query(SubProjectGroup).filter(SubProjectGroup.id.in_(group_ids)).delete(synchronize_session=False)

    db.delete(pp)
    db.commit()

    return None


@router.get("/{parent_project_id}/context", response_model=dict, dependencies=[Depends(require_role("admin", "pm"))])
def get_parent_context(parent_project_id: int, db: Session = Depends(get_db)):
    """
    Get context for inheriting into a new sub-project.
    Returns PM details and client info for auto-population.
    """
    pp = db.query(ParentProject).filter(ParentProject.id == parent_project_id).first()
    
    if not pp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Parent project with ID {parent_project_id} not found"
        )
    
    return {
        "program_manager_id": pp.program_manager_id,
        "program_manager_name": get_pm_name(db, pp.program_manager_id),
        "program_manager_ids": get_pm_ids(pp),
        "program_manager_names": get_pm_names(db, get_pm_ids(pp)),
        "client": pp.client,
        "parent_name": pp.name,
        "global_start_date": pp.global_start_date.isoformat() if pp.global_start_date else None
    }


@router.get("/{parent_project_id}/clone-suggestions", response_model=dict, dependencies=[Depends(require_role("admin", "pm"))])
def get_clone_suggestions(parent_project_id: int, db: Session = Depends(get_db)):
    """
    Get allocation suggestions from the most recent sibling project (optimized).
    Implements the "Placeholder" Smart Cloning logic.
    """
    from app.models.allocation import Allocation
    
    # Get the most recent sub-project
    latest_sibling = db.query(Project).filter(
        Project.main_project_id == parent_project_id
    ).order_by(Project.created_at.desc()).first()
    
    if not latest_sibling:
        return {
            "has_suggestions": False,
            "sibling_project_id": None,
            "sibling_project_name": None,
            "suggested_allocations": []
        }
    
    # Get allocations from the sibling
    allocations = db.query(Allocation).filter(
        Allocation.sub_project_id == latest_sibling.id
    ).all()
    
    if not allocations:
        return {
            "has_suggestions": False,
            "sibling_project_id": latest_sibling.id,
            "sibling_project_name": latest_sibling.name,
            "suggested_allocations": []
        }
    
    # Batch load all employees for these allocations in a single query
    employee_ids = list(set(alloc.employee_id for alloc in allocations))
    employees = db.query(Employee).filter(
        Employee.id.in_(employee_ids),
        Employee.status == 'active'  # Filter active employees in the query
    ).all()
    employee_map = {emp.id: emp for emp in employees}
    
    suggested = []
    for alloc in allocations:
        employee = employee_map.get(alloc.employee_id)
        if employee:
            suggested.append({
                "employee_id": alloc.employee_id,
                "employee_name": employee.name,
                "total_daily_hours": getattr(alloc, 'total_daily_hours', 8),
                "role_tags": getattr(alloc, 'role_tags', []),
                "status": "suggested"
            })
    
    return {
        "has_suggestions": len(suggested) > 0,
        "sibling_project_id": latest_sibling.id,
        "sibling_project_name": latest_sibling.name,
        "suggested_allocations": suggested
    }
