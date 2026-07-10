"""
Project-based monthly self-evaluations.

Flow:
- PM defines per-project parameter names (GET/PUT /params).
- Employee submits a monthly self-evaluation for a project (POST). Locked after submit.
- PM reviews: edit (PUT) or accept (PATCH /accept).
- Admin views accepted summaries (GET with status filter).
"""
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.perf_eval import PerfProjectParams, PerfEvaluation

router = APIRouter(prefix="/api/perf-evals", tags=["Performance Evaluations"])

PERIOD_OK = lambda v: isinstance(v, str) and len(v) == 7 and v[4] == "-" and v[:4].isdigit() and v[5:].isdigit() and 1 <= int(v[5:]) <= 12


# ── Project parameter template ───────────────────────────────────────────────
class ProjectParamsPayload(BaseModel):
    project_id: int
    params: List[str]

    @field_validator("params")
    @classmethod
    def clean_params(cls, v):
        # Reserved names are rendered as separate fixed fields — exclude them.
        reserved = {
            "overall contributions in last month",
            "areas that are your strengths",
            "areas to improve",
        }
        cleaned = [p.strip() for p in v if isinstance(p, str) and p.strip()]
        # De-duplicate preserving order, drop reserved
        seen = set()
        out = []
        for p in cleaned:
            key = p.lower()
            if key in reserved or key in seen:
                continue
            seen.add(key)
            out.append(p)
        return out


class ProjectParamsResponse(BaseModel):
    project_id: int
    params: List[str]

    class Config:
        from_attributes = True


@router.get("/params/{project_id}", response_model=ProjectParamsResponse)
def get_project_params(project_id: int, db: Session = Depends(get_db)):
    row = db.query(PerfProjectParams).filter(PerfProjectParams.project_id == project_id).first()
    if not row:
        return ProjectParamsResponse(project_id=project_id, params=[])
    return row


@router.put("/params", response_model=ProjectParamsResponse)
def set_project_params(payload: ProjectParamsPayload, db: Session = Depends(get_db)):
    row = db.query(PerfProjectParams).filter(PerfProjectParams.project_id == payload.project_id).first()
    if row:
        row.params = payload.params
    else:
        row = PerfProjectParams(project_id=payload.project_id, params=payload.params)
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ── Evaluations ──────────────────────────────────────────────────────────────
class ParamValue(BaseModel):
    name: str
    value: Optional[str] = None


class PerfEvalCreate(BaseModel):
    project_id: int
    employee_id: int
    period: str
    parameter_values: List[ParamValue] = []
    contributions: Optional[str] = None
    strengths: Optional[str] = None
    improvements: Optional[str] = None
    overall_rating: Optional[float] = None
    submitted_by: Optional[int] = None

    @field_validator("period")
    @classmethod
    def check_period(cls, v):
        if not PERIOD_OK(v):
            raise ValueError("period must be in YYYY-MM format")
        return v

    @field_validator("overall_rating")
    @classmethod
    def check_rating(cls, v):
        if v is not None and not (1.0 <= v <= 5.0):
            raise ValueError("overall_rating must be between 1 and 5")
        return v


class PerfEvalUpdate(BaseModel):
    parameter_values: Optional[List[ParamValue]] = None
    contributions: Optional[str] = None
    strengths: Optional[str] = None
    improvements: Optional[str] = None
    overall_rating: Optional[float] = None
    reviewed_by: Optional[int] = None

    @field_validator("overall_rating")
    @classmethod
    def check_rating(cls, v):
        if v is not None and not (1.0 <= v <= 5.0):
            raise ValueError("overall_rating must be between 1 and 5")
        return v


class PerfEvalResponse(BaseModel):
    id: int
    project_id: int
    employee_id: int
    period: str
    parameter_values: List[Dict[str, Any]]
    contributions: Optional[str] = None
    strengths: Optional[str] = None
    improvements: Optional[str] = None
    overall_rating: Optional[float] = None
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

    ev = PerfEvaluation(
        project_id=payload.project_id,
        employee_id=payload.employee_id,
        period=payload.period,
        parameter_values=[pv.model_dump() for pv in payload.parameter_values],
        contributions=payload.contributions,
        strengths=payload.strengths,
        improvements=payload.improvements,
        overall_rating=payload.overall_rating,
        status="submitted",
        submitted_by=payload.submitted_by,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


@router.put("/{eval_id}", response_model=PerfEvalResponse)
def update_eval(eval_id: int, payload: PerfEvalUpdate, db: Session = Depends(get_db)):
    ev = db.query(PerfEvaluation).filter(PerfEvaluation.id == eval_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluation not found")

    data = payload.model_dump(exclude_unset=True)
    if "parameter_values" in data and data["parameter_values"] is not None:
        ev.parameter_values = [dict(pv) for pv in data["parameter_values"]]
    for field in ("contributions", "strengths", "improvements", "overall_rating", "reviewed_by"):
        if field in data:
            setattr(ev, field, data[field])
    db.commit()
    db.refresh(ev)
    return ev


@router.patch("/{eval_id}/accept", response_model=PerfEvalResponse)
def accept_eval(eval_id: int, reviewed_by: Optional[int] = None, db: Session = Depends(get_db)):
    ev = db.query(PerfEvaluation).filter(PerfEvaluation.id == eval_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    ev.status = "accepted"
    if reviewed_by is not None:
        ev.reviewed_by = reviewed_by
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
