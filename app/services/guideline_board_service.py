"""
Guideline Board Service
Server-side paginated + joined listing for the Guidelines page. Resolves
organization_name and project_name via SQL JOINs instead of the frontend
downloading full project/org lists to resolve names client-side.
"""
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.guideline import Guideline
from app.models.parent_project import MainProject
from app.models.project import Project  # backs the "sub-project" UI concept


class GuidelinePageItem(BaseModel):
    id: int
    title: str
    content: Optional[str] = None
    file_name: Optional[str] = None
    file_url: Optional[str] = None
    main_project_id: Optional[int] = None
    organization_name: Optional[str] = None
    sub_project_id: Optional[int] = None
    project_name: Optional[str] = None
    uploaded_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class GuidelinesPageResponse(BaseModel):
    items: List[GuidelinePageItem]
    page: int
    page_size: int
    total_items: int
    total_pages: int


def get_guidelines_page(
    db: Session,
    page: int,
    page_size: int,
    search: Optional[str],
    main_project_id: Optional[int],
    sub_project_id: Optional[int],
    uploaded_by: Optional[int],
) -> GuidelinesPageResponse:
    # Single query with JOINs — organization_name and project_name are resolved
    # in SQL, not by looping over rows and querying MainProject/Project per guideline.
    query = (
        db.query(
            Guideline,
            MainProject.name.label("organization_name"),
            Project.name.label("project_name"),
        )
        .outerjoin(MainProject, Guideline.main_project_id == MainProject.id)
        .outerjoin(Project, Guideline.sub_project_id == Project.id)
    )

    if main_project_id:
        query = query.filter(Guideline.main_project_id == main_project_id)
    if sub_project_id:
        query = query.filter(Guideline.sub_project_id == sub_project_id)
    if uploaded_by:
        query = query.filter(Guideline.uploaded_by == uploaded_by)

    if search and search.strip():
        # Simple ILIKE for now — see note below on GIN/tsvector for scale.
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Guideline.title.ilike(term),
                Guideline.content.ilike(term),
                Guideline.file_name.ilike(term),
            )
        )

    query = query.order_by(Guideline.created_at.desc())

    total_items = query.count()
    total_pages = (total_items + page_size - 1) // page_size if page_size else 0

    rows = query.offset((page - 1) * page_size).limit(page_size).all()

    items = [
        GuidelinePageItem(
            id=g.id,
            title=g.title,
            content=g.content,
            file_name=g.file_name,
            file_url=g.file_url,
            main_project_id=g.main_project_id,
            organization_name=org_name,
            sub_project_id=g.sub_project_id,
            project_name=proj_name,
            uploaded_by=g.uploaded_by,
            created_at=g.created_at,
            updated_at=g.updated_at,
        )
        for (g, org_name, proj_name) in rows
    ]

    return GuidelinesPageResponse(
        items=items, page=page, page_size=page_size,
        total_items=total_items, total_pages=total_pages,
    )