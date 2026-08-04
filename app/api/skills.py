from fastapi import APIRouter, Depends, HTTPException, Request
from app.services.auth_service import get_current_user
from app.services import audit_service
from app.models.user import User
from sqlalchemy.orm import Session
from typing import List

from app.db.database import get_db
from app.schemas.skill import Skill, SkillCreate
from app.services import skill as skill_crud
from app.seed_skills import ALLOWED_SKILLS, seed_skills

router = APIRouter(prefix="/api/skills", tags=["skills"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=List[Skill])
def get_skills(db: Session = Depends(get_db)):
    """Get all skills"""
    # Self-heal and prune the catalog to the approved dropdown values.
    seed_skills()
    skills = skill_crud.get_all_skills(db)
    return [skill for skill in skills if skill.name in ALLOWED_SKILLS]


@router.get("/summary")
def get_skills_summary(db: Session = Depends(get_db)):
    """Get manpower summary by skill"""
    from collections import defaultdict
    from app.models.employee import Employee
    from app.models.allocation import Allocation
    
    employees = db.query(Employee).filter(Employee.status == "active").all()
    allocations = db.query(Allocation).all()
    allocated_employee_ids = {a.employee_id for a in allocations}
    
    skill_summary = defaultdict(lambda: {"total": 0, "allocated": 0, "available": 0})
    
    for emp in employees:
        if emp.skills:
            for skill in emp.skills:
                skill_lower = skill.lower().strip()
                skill_summary[skill_lower]["total"] += 1
                
                if emp.id in allocated_employee_ids:
                    skill_summary[skill_lower]["allocated"] += 1
                else:
                    skill_summary[skill_lower]["available"] += 1
    
    return {
        "skills": dict(skill_summary),
        "total_active_employees": len(employees),
        "total_allocated": len(allocated_employee_ids),
        "total_available": len(employees) - len([e for e in employees if e.id in allocated_employee_ids])
    }


@router.post("", response_model=Skill)
def create_skill(
    skill: SkillCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new skill"""
    if skill.name not in ALLOWED_SKILLS:
        raise HTTPException(status_code=400, detail="Only approved skills are allowed")

    # Check if skill already exists
    existing_skill = skill_crud.get_skill_by_name(db, skill.name)
    if existing_skill:
        raise HTTPException(status_code=400, detail="Skill already exists")

    created = skill_crud.create_skill(db, skill)

    # skill_crud.create_skill commits internally, so this needs its own commit —
    # there is no later one to ride along on.
    audit_service.record(
        db,
        actor=current_user,
        action="skill.created",
        category="Skills",
        action_type="Created",
        entity_type="skill",
        entity_id=created.id,
        entity_name=created.name,
        summary=f"Added skill {created.name} to the catalog",
        request=http_request,
    )
    db.commit()
    return created


@router.delete("/{skill_id}")
def delete_skill(
    skill_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a skill"""
    skill = skill_crud.delete_skill(db, skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    audit_service.record(
        db,
        actor=current_user,
        action="skill.deleted",
        category="Skills",
        action_type="Deleted",
        entity_type="skill",
        entity_id=skill_id,
        entity_name=getattr(skill, "name", None),
        summary=f"Removed skill {getattr(skill, 'name', skill_id)} from the catalog",
        request=http_request,
    )
    db.commit()
    return {"message": "Skill deleted"}
