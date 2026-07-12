"""
Project-based monthly performance evaluations.

Flow:
- The five parameters are fixed (app/constants/perf_params.py) — no PM setup.
- Employee submits a monthly evaluation for a project, rating each parameter 1-5
  (POST). Locked after submit.
- PM reviews (PATCH /{id}/review): approves/rejects each parameter, assigns their
  own 1-5 rating, leaves feedback on rejected ones, and may suggest a bonus.
  PM ratings drive overall_rating and status becomes "reviewed".
- Admin views all evaluations (GET, optional status filter).
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.constants.perf_params import PERF_PARAM_NAME_SET, RATING_MIN, RATING_MAX
from app.db.database import get_db
from app.models.perf_eval import PerfEvaluation

router = APIRouter(prefix="/api/perf-evals", tags=["Performance Evaluations"])

PERIOD_OK = lambda v: isinstance(v, str) and len(v) == 7 and v[4] == "-" and v[:4].isdigit() and v[5:].isdigit() and 1 <= int(v[5:]) <= 12


def _mean(values: List[float]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


# ── Employee submission ──────────────────────────────────────────────────────
class EmployeeParamValue(BaseModel):
    name: str
    employee_rating: int

    @field_validator("employee_rating")
    @classmethod
    def check_rating(cls, v):
        if not (RATING_MIN <= v <= RATING_MAX):
            raise ValueError(f"employee_rating must be between {RATING_MIN} and {RATING_MAX}")
        return v


class PerfEvalCreate(BaseModel):
    project_id: int
    employee_id: int
    period: str
    parameter_values: List[EmployeeParamValue]
    overall_comment: Optional[str] = None
    submitted_by: Optional[int] = None

    @field_validator("period")
    @classmethod
    def check_period(cls, v):
        if not PERIOD_OK(v):
            raise ValueError("period must be in YYYY-MM format")
        return v

    @field_validator("parameter_values")
    @classmethod
    def check_params(cls, v):
        names = {p.name for p in v}
        if names != PERF_PARAM_NAME_SET:
            raise ValueError("parameter_values must cover exactly the five fixed parameters")
        return v


# ── PM review ────────────────────────────────────────────────────────────────
class ReviewParamValue(BaseModel):
    name: str
    pm_rating: int
    approved: bool = True
    feedback: Optional[str] = None

    @field_validator("pm_rating")
    @classmethod
    def check_rating(cls, v):
        if not (RATING_MIN <= v <= RATING_MAX):
            raise ValueError(f"pm_rating must be between {RATING_MIN} and {RATING_MAX}")
        return v


class PerfEvalReview(BaseModel):
    parameter_values: List[ReviewParamValue]
    bonus_suggested: bool = False
    bonus_note: Optional[str] = None
    reviewed_by: Optional[int] = None

    @field_validator("parameter_values")
    @classmethod
    def check_params(cls, v):
        names = {p.name for p in v}
        if names != PERF_PARAM_NAME_SET:
            raise ValueError("parameter_values must cover exactly the five fixed parameters")
        for p in v:
            if not p.approved and not (p.feedback and p.feedback.strip()):
                raise ValueError(f"feedback is required for rejected parameter '{p.name}'")
        return v


class PerfEvalResponse(BaseModel):
    id: int
    project_id: int
    employee_id: int
    period: str
    parameter_values: List[Dict[str, Any]]
    overall_comment: Optional[str] = None
    employee_overall_rating: Optional[float] = None
    overall_rating: Optional[float] = None
    bonus_suggested: bool = False
    bonus_note: Optional[str] = None
    status: str
    submitted_by: Optional[int] = None
    reviewed_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


@router.get("", response_model=List[PerfEvalResponse])
def list_evals(
    project_id: Optional[int] = None,
    employee_id: Optional[int] = None,
    period: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(PerfEvaluation)
    if project_id:
        q = q.filter(PerfEvaluation.project_id == project_id)
    if employee_id:
        q = q.filter(PerfEvaluation.employee_id == employee_id)
    if period:
        q = q.filter(PerfEvaluation.period == period)
    if status:
        q = q.filter(PerfEvaluation.status == status)
    return q.order_by(PerfEvaluation.period.desc(), PerfEvaluation.created_at.desc()).all()


@router.post("", response_model=PerfEvalResponse, status_code=201)
def create_eval(payload: PerfEvalCreate, db: Session = Depends(get_db)):
    existing = (
        db.query(PerfEvaluation)
        .filter(
            PerfEvaluation.project_id == payload.project_id,
            PerfEvaluation.employee_id == payload.employee_id,
            PerfEvaluation.period == payload.period,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail="You have already submitted an evaluation for this project and month.",
        )

    param_values = [
        {"name": pv.name, "employee_rating": pv.employee_rating, "pm_rating": None, "approved": None, "feedback": None}
        for pv in payload.parameter_values
    ]

    ev = PerfEvaluation(
        project_id=payload.project_id,
        employee_id=payload.employee_id,
        period=payload.period,
        parameter_values=param_values,
        overall_comment=(payload.overall_comment or None),
        employee_overall_rating=_mean([pv.employee_rating for pv in payload.parameter_values]),
        status="submitted",
        submitted_by=payload.submitted_by,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


@router.patch("/{eval_id}/review", response_model=PerfEvalResponse)
def review_eval(eval_id: int, payload: PerfEvalReview, db: Session = Depends(get_db)):
    ev = db.query(PerfEvaluation).filter(PerfEvaluation.id == eval_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluation not found")

    # Merge the PM's review into the existing (employee-supplied) parameter rows.
    review_by_name = {p.name: p for p in payload.parameter_values}
    merged = []
    for row in (ev.parameter_values or []):
        name = row.get("name")
        r = review_by_name.get(name)
        if r is not None:
            merged.append({
                "name": name,
                "employee_rating": row.get("employee_rating"),
                "pm_rating": r.pm_rating,
                "approved": r.approved,
                "feedback": (r.feedback.strip() if (r.feedback and not r.approved) else None),
            })
        else:
            merged.append(row)

    ev.parameter_values = merged
    ev.overall_rating = _mean([p.pm_rating for p in payload.parameter_values])
    ev.bonus_suggested = bool(payload.bonus_suggested)
    ev.bonus_note = (payload.bonus_note.strip() if payload.bonus_note else None)
    ev.status = "reviewed"
    if payload.reviewed_by is not None:
        ev.reviewed_by = payload.reviewed_by
    db.commit()
    db.refresh(ev)
    return ev


@router.delete("/{eval_id}")
def delete_eval(eval_id: int, db: Session = Depends(get_db)):
    ev = db.query(PerfEvaluation).filter(PerfEvaluation.id == eval_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    db.delete(ev)
    db.commit()
    return {"message": "Evaluation deleted successfully"}
