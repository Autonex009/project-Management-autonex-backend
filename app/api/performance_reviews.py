"""
Performance Reviews API — PMs can add feedback, reviews, and comments for employees.
"""
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from app.services.auth_service import get_current_user, require_role
from app.services import project_scope
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.performance_review import PerformanceReview
from app.models.user import User

router = APIRouter(prefix="/api/performance-reviews", tags=["Performance Reviews"], dependencies=[Depends(require_role("admin", "pm"))])

ReviewType = Literal["feedback", "performance_review", "comment"]


class PerformanceReviewCreate(BaseModel):
    employee_id: int
    reviewer_id: Optional[int] = None
    review_type: ReviewType = "feedback"
    title: str
    content: str
    rating: Optional[float] = None
    period: Optional[str] = None

    @field_validator("rating")
    @classmethod
    def validate_rating(cls, v):
        if v is not None and not (1.0 <= v <= 5.0):
            raise ValueError("Rating must be between 1 and 5")
        return v


class PerformanceReviewUpdate(BaseModel):
    review_type: Optional[ReviewType] = None
    title: Optional[str] = None
    content: Optional[str] = None
    rating: Optional[float] = None
    period: Optional[str] = None

    @field_validator("rating")
    @classmethod
    def validate_rating(cls, v):
        if v is not None and not (1.0 <= v <= 5.0):
            raise ValueError("Rating must be between 1 and 5")
        return v


class PerformanceReviewResponse(BaseModel):
    id: int
    employee_id: int
    reviewer_id: Optional[int] = None
    review_type: str
    title: str
    content: str
    rating: Optional[float] = None
    period: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


@router.get("", response_model=list[PerformanceReviewResponse])
def list_reviews(
    employee_id: Optional[int] = None,
    reviewer_id: Optional[int] = None,
    review_type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(PerformanceReview)
    if employee_id:
        query = query.filter(PerformanceReview.employee_id == employee_id)
    if reviewer_id:
        query = query.filter(PerformanceReview.reviewer_id == reviewer_id)
    if review_type:
        query = query.filter(PerformanceReview.review_type == review_type)
 
    # Same pattern as perf_eval.list_evals: admins/HR see everything; a PM only sees
    # reviews for employees they actually manage. Without this, `employee_id` filter
    # is optional and omitting it returned the whole company's reviews to any PM.
    if not project_scope.has_full_access(current_user):
        all_reviews = query.order_by(PerformanceReview.created_at.desc()).all()
        manageable_cache: dict[int, bool] = {}
        filtered_reviews = []
        for review in all_reviews:
            if review.employee_id not in manageable_cache:
                manageable_cache[review.employee_id] = project_scope.can_manage_employee(
                    db, current_user, review.employee_id
                )
            if manageable_cache[review.employee_id]:
                filtered_reviews.append(review)
 
        return filtered_reviews
    else:
        return query.order_by(PerformanceReview.created_at.desc()).all()
 



@router.get("/{review_id}", response_model=PerformanceReviewResponse)
def get_review(
    review_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    review = db.query(PerformanceReview).filter(PerformanceReview.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")

    project_scope.require_employee_scope(
        db, current_user, review.employee_id, action="view a review"
    )

    return review


@router.post("", response_model=PerformanceReviewResponse, status_code=201)
def create_review(
    payload: PerformanceReviewCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project_scope.require_employee_scope(
        db, current_user, payload.employee_id, action="create a review"
    )

    # reviewer_id comes from the session, not the body — it records who authored this
    # assessment of someone's performance and must not be settable by the caller.
    data = payload.model_dump()
    data["reviewer_id"] = current_user.id
    review = PerformanceReview(**data)
    db.add(review)
    db.commit()
    db.refresh(review)
    return review


@router.put("/{review_id}", response_model=PerformanceReviewResponse)
def update_review(
    review_id: int,
    payload: PerformanceReviewUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    review = db.query(PerformanceReview).filter(PerformanceReview.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")

    project_scope.require_employee_scope(
        db, current_user, review.employee_id, action="update a review"
    )

    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(review, key, value)

    db.commit()
    db.refresh(review)
    return review


@router.delete("/{review_id}")
def delete_review(
    review_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    review = db.query(PerformanceReview).filter(PerformanceReview.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")

    project_scope.require_employee_scope(
        db, current_user, review.employee_id, action="delete a review"
    )

    db.delete(review)
    db.commit()
    return {"message": "Review deleted successfully"}