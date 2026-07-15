"""
Performance Reviews API — PMs can add feedback, reviews, and comments for employees.
"""
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from app.services.auth_service import get_current_user, require_role
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.performance_review import PerformanceReview

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


@router.get("", response_model=List[PerformanceReviewResponse])
def list_reviews(
    employee_id: Optional[int] = None,
    reviewer_id: Optional[int] = None,
    review_type: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = db.query(PerformanceReview)
    if employee_id:
        query = query.filter(PerformanceReview.employee_id == employee_id)
    if reviewer_id:
        query = query.filter(PerformanceReview.reviewer_id == reviewer_id)
    if review_type:
        query = query.filter(PerformanceReview.review_type == review_type)
    return query.order_by(PerformanceReview.created_at.desc()).all()


@router.get("/{review_id}", response_model=PerformanceReviewResponse)
def get_review(review_id: int, db: Session = Depends(get_db)):
    review = db.query(PerformanceReview).filter(PerformanceReview.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    return review


@router.post("", response_model=PerformanceReviewResponse, status_code=201)
def create_review(payload: PerformanceReviewCreate, db: Session = Depends(get_db)):
    review = PerformanceReview(**payload.model_dump())
    db.add(review)
    db.commit()
    db.refresh(review)
    return review


@router.put("/{review_id}", response_model=PerformanceReviewResponse)
def update_review(review_id: int, payload: PerformanceReviewUpdate, db: Session = Depends(get_db)):
    review = db.query(PerformanceReview).filter(PerformanceReview.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(review, key, value)
    db.commit()
    db.refresh(review)
    return review


@router.delete("/{review_id}")
def delete_review(review_id: int, db: Session = Depends(get_db)):
    review = db.query(PerformanceReview).filter(PerformanceReview.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    db.delete(review)
    db.commit()
    return {"message": "Review deleted successfully"}
