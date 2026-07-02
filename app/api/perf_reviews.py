"""
Monthly structured performance reviews.

Each review scores five fixed criteria (1–5) for an employee for a given month
(period "YYYY-MM"). PMs review their team; admins review anyone (incl. PMs).
Only one review per employee per month — creating for an existing month updates it.
"""
import re
from datetime import datetime
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.perf_review import PerfReview

router = APIRouter(prefix="/api/perf-reviews", tags=["Performance Reviews"])

# Fixed set of criteria (each rated 1–5).
PERFORMANCE_CRITERIA = ("quality", "productivity", "communication", "teamwork", "initiative")

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

ReviewerRole = Literal["pm", "admin"]


def _validate_criteria(v: Dict[str, int]) -> Dict[str, int]:
    if not isinstance(v, dict):
        raise ValueError("criteria_ratings must be an object")
    missing = [c for c in PERFORMANCE_CRITERIA if c not in v or v[c] is None]
    if missing:
        raise ValueError(f"All criteria are required. Missing: {', '.join(missing)}")
    cleaned: Dict[str, int] = {}
    for key, value in v.items():
        if key not in PERFORMANCE_CRITERIA:
            raise ValueError(f"Unknown criterion '{key}'. Allowed: {', '.join(PERFORMANCE_CRITERIA)}")
        if not (1 <= int(value) <= 5):
            raise ValueError("Each criterion rating must be between 1 and 5")
        cleaned[key] = int(value)
    return cleaned


def _compute_average(criteria: Dict[str, int]) -> float:
    values = [criteria[c] for c in PERFORMANCE_CRITERIA]
    return round(sum(values) / len(values), 2)


class PerfReviewCreate(BaseModel):
    employee_id: int
    reviewer_id: Optional[int] = None
    reviewer_role: Optional[ReviewerRole] = None
    criteria_ratings: Dict[str, int]
    comment: Optional[str] = None
    period: str

    @field_validator("criteria_ratings")
    @classmethod
    def check_criteria(cls, v):
        return _validate_criteria(v)

    @field_validator("period")
    @classmethod
    def check_period(cls, v):
        if not PERIOD_RE.match(v or ""):
            raise ValueError("period must be in YYYY-MM format")
        return v


class PerfReviewUpdate(BaseModel):
    criteria_ratings: Optional[Dict[str, int]] = None
    comment: Optional[str] = None
    period: Optional[str] = None

    @field_validator("criteria_ratings")
    @classmethod
    def check_criteria(cls, v):
        if v is None:
            return v
        return _validate_criteria(v)

    @field_validator("period")
    @classmethod
    def check_period(cls, v):
        if v is not None and not PERIOD_RE.match(v):
            raise ValueError("period must be in YYYY-MM format")
        return v


class PerfReviewResponse(BaseModel):
    id: int
    employee_id: int
    reviewer_id: Optional[int] = None
    reviewer_role: Optional[str] = None
    criteria_ratings: Dict[str, int]
    average: Optional[float] = None
    comment: Optional[str] = None
    period: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


@router.get("", response_model=List[PerfReviewResponse])
def list_reviews(
    employee_id: Optional[int] = None,
    reviewer_id: Optional[int] = None,
    period: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = db.query(PerfReview)
    if employee_id:
        query = query.filter(PerfReview.employee_id == employee_id)
    if reviewer_id:
        query = query.filter(PerfReview.reviewer_id == reviewer_id)
    if period:
        query = query.filter(PerfReview.period == period)
    return query.order_by(PerfReview.period.desc(), PerfReview.created_at.desc()).all()


@router.get("/{review_id}", response_model=PerfReviewResponse)
def get_review(review_id: int, db: Session = Depends(get_db)):
    review = db.query(PerfReview).filter(PerfReview.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    return review


@router.post("", response_model=PerfReviewResponse, status_code=201)
def create_review(payload: PerfReviewCreate, db: Session = Depends(get_db)):
    # Monthly single review: if one already exists for this employee+month, update it.
    existing = (
        db.query(PerfReview)
        .filter(PerfReview.employee_id == payload.employee_id, PerfReview.period == payload.period)
        .first()
    )
    if existing:
        existing.criteria_ratings = payload.criteria_ratings
        existing.average = _compute_average(payload.criteria_ratings)
        existing.comment = payload.comment
        if payload.reviewer_id is not None:
            existing.reviewer_id = payload.reviewer_id
        if payload.reviewer_role is not None:
            existing.reviewer_role = payload.reviewer_role
        db.commit()
        db.refresh(existing)
        return existing

    review = PerfReview(
        employee_id=payload.employee_id,
        reviewer_id=payload.reviewer_id,
        reviewer_role=payload.reviewer_role,
        criteria_ratings=payload.criteria_ratings,
        average=_compute_average(payload.criteria_ratings),
        comment=payload.comment,
        period=payload.period,
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return review


@router.put("/{review_id}", response_model=PerfReviewResponse)
def update_review(review_id: int, payload: PerfReviewUpdate, db: Session = Depends(get_db)):
    review = db.query(PerfReview).filter(PerfReview.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")

    data = payload.model_dump(exclude_unset=True)
    if "criteria_ratings" in data and data["criteria_ratings"] is not None:
        review.criteria_ratings = data["criteria_ratings"]
        review.average = _compute_average(data["criteria_ratings"])
    if "comment" in data:
        review.comment = data["comment"]
    if "period" in data and data["period"] is not None:
        review.period = data["period"]

    db.commit()
    db.refresh(review)
    return review


@router.delete("/{review_id}")
def delete_review(review_id: int, db: Session = Depends(get_db)):
    review = db.query(PerfReview).filter(PerfReview.id == review_id).first()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    db.delete(review)
    db.commit()
    return {"message": "Review deleted successfully"}
