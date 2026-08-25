from sqlalchemy.orm import Session
from app.models.skill import Skill
from app.schemas.skill import SkillCreate


def get_all_skills(db: Session):
    """Get all skills"""
    return db.query(Skill).order_by(Skill.name).all()


def get_skill_by_name(db: Session, name: str):
    """Get skill by name"""
    return db.query(Skill).filter(Skill.name == name).first()


def create_skill(db: Session, skill: SkillCreate):
    """Create a new skill.

    Does **not** commit — stages the row and flushes so ``id`` is available.
    Caller owns the transaction (and any audit write) and must commit.
    """
    db_skill = Skill(name=skill.name)
    db.add(db_skill)
    db.flush()
    db.refresh(db_skill)
    return db_skill


def create_skill_if_not_exists(db: Session, name: str):
    """Create skill if it doesn't exist, return existing if it does.

    Does not commit when creating — same transaction rules as ``create_skill``.
    """
    existing_skill = get_skill_by_name(db, name)
    if existing_skill:
        return existing_skill

    skill = SkillCreate(name=name)
    return create_skill(db, skill)


def delete_skill(db: Session, skill_id: int):
    """Delete a skill by ID.

    Does **not** commit — stages the delete. Caller owns the transaction
    (and any audit write) and must commit.
    """
    skill = db.query(Skill).filter(Skill.id == skill_id).first()
    if skill:
        db.delete(skill)
        db.flush()
    return skill