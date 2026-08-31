"""
Onboarding Pipeline API — manages the 5-day shadow/training workflow.

Endpoints:
  POST /api/onboarding/pipeline/assign    – PM assigns candidate to project + buddy
  POST /api/onboarding/pipeline/confirm   – Candidate accepts the assignment
  GET  /api/onboarding/pipeline           – Dashboard roster (all pipeline records)
  POST /api/onboarding/pipeline/{id}/evaluate – Buddy submits Day-5 evaluation
"""
from datetime import datetime, date
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.onboarding_pipeline import OnboardingPipeline
from app.models.user import User
from app.models.employee import Employee
from app.models.parent_project import ParentProject
from app.models.allocation import Allocation
from app.schemas.onboarding_pipeline import (
    PipelineAssignRequest,
    PipelineBulkAssignRequest,
    PipelineEvaluateRequest,
    PipelineResponse,
)
from app.services.auth_service import get_current_user
from app.services.email_service import (
    try_send_evaluation_passed_email,
    try_send_evaluation_failed_email,
)

router = APIRouter(
    prefix="/api/onboarding/pipeline",
    tags=["Onboarding Pipeline"],
    dependencies=[Depends(get_current_user)],
)


# ── Helpers ──────────────────────────────────────────────────────────

def _enrich_pipeline_record(record: OnboardingPipeline, db: Session) -> PipelineResponse:
    """Join in candidate name, project name, buddy name, and compute days_elapsed."""
    candidate = db.query(User).filter(User.id == record.candidate_id).first()
    project = db.query(ParentProject).filter(ParentProject.id == record.project_id).first() if record.project_id else None
    buddy = db.query(Employee).filter(Employee.id == record.buddy_id).first() if record.buddy_id else None

    days_elapsed = None
    if record.started_at:
        delta = datetime.utcnow() - record.started_at
        days_elapsed = delta.days

    return PipelineResponse(
        id=record.id,
        candidate_id=record.candidate_id,
        candidate_name=candidate.name if candidate else None,
        candidate_email=candidate.email if candidate else None,
        project_id=record.project_id,
        project_name=project.name if project else None,
        buddy_id=record.buddy_id,
        buddy_name=buddy.name if buddy else None,
        status=record.status,
        days_elapsed=days_elapsed,
        started_at=record.started_at,
        evaluated_at=record.evaluated_at,
        eval_score=record.eval_score,
        eval_notes=record.eval_notes,
        created_at=record.created_at,
    )


def _require_admin_or_hr(current_user: User) -> None:
    if current_user.role not in ("admin", "hr", "pm"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin, HR, or PM users can perform this action.",
        )


# ── Endpoints ────────────────────────────────────────────────────────

@router.post("/assign", status_code=status.HTTP_201_CREATED)
def assign_candidate(
    payload: PipelineAssignRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """PM/HR assigns a candidate to a project and buddy. Creates a pipeline record."""
    _require_admin_or_hr(current_user)

    # Validate candidate exists
    candidate = db.query(User).filter(User.id == payload.candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found.")

    # Check for duplicate active pipeline
    existing = db.query(OnboardingPipeline).filter(
        OnboardingPipeline.candidate_id == payload.candidate_id,
        OnboardingPipeline.status.in_(["pending_confirmation", "in_progress", "day_5_pending"]),
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Candidate already has an active pipeline (status: {existing.status}).",
        )

    # Validate project exists
    project = db.query(ParentProject).filter(ParentProject.id == payload.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")

    # Validate buddy exists
    buddy = db.query(Employee).filter(Employee.id == payload.buddy_id).first()
    if not buddy:
        raise HTTPException(status_code=404, detail="Buddy/Mentor not found.")

    record = OnboardingPipeline(
        candidate_id=payload.candidate_id,
        project_id=payload.project_id,
        buddy_id=payload.buddy_id,
        status="pending_confirmation",
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    return _enrich_pipeline_record(record, db)


@router.post("/bulk-assign", status_code=status.HTTP_201_CREATED)
def bulk_assign_candidates(
    payload: PipelineBulkAssignRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """PM/HR assigns multiple candidates to a project and buddy in a single transaction."""
    _require_admin_or_hr(current_user)

    # Validate project exists
    project = db.query(ParentProject).filter(ParentProject.id == payload.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")

    # Validate buddy exists
    buddy = db.query(Employee).filter(Employee.id == payload.buddy_id).first()
    if not buddy:
        raise HTTPException(status_code=404, detail="Buddy/Mentor not found.")

    # Fetch candidates
    candidates = db.query(User).filter(User.id.in_(payload.candidate_ids)).all()
    found_ids = {c.id for c in candidates}
    
    missing_ids = set(payload.candidate_ids) - found_ids
    if missing_ids:
        raise HTTPException(status_code=404, detail=f"Candidates not found: {missing_ids}")

    # Check for existing active pipelines
    existing = db.query(OnboardingPipeline).filter(
        OnboardingPipeline.candidate_id.in_(payload.candidate_ids),
        OnboardingPipeline.status.in_(["pending_confirmation", "in_progress", "day_5_pending"]),
    ).all()
    
    if existing:
        existing_candidate_ids = {e.candidate_id for e in existing}
        raise HTTPException(
            status_code=409,
            detail=f"Candidates {existing_candidate_ids} already have an active pipeline.",
        )

    # Create records
    records = []
    for cid in payload.candidate_ids:
        record = OnboardingPipeline(
            candidate_id=cid,
            project_id=payload.project_id,
            buddy_id=payload.buddy_id,
            status="pending_confirmation",
        )
        db.add(record)
        records.append(record)

    db.commit()
    
    # Refresh records to get their IDs
    for r in records:
        db.refresh(r)

    return [_enrich_pipeline_record(r, db) for r in records]


@router.post("/confirm")
def confirm_onboarding(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Candidate accepts the assignment. Starts the 5-day clock."""
    record = db.query(OnboardingPipeline).filter(
        OnboardingPipeline.candidate_id == current_user.id,
        OnboardingPipeline.status == "pending_confirmation",
    ).first()

    if not record:
        raise HTTPException(
            status_code=404,
            detail="No pending onboarding confirmation found for your account.",
        )

    record.status = "in_progress"
    record.started_at = datetime.utcnow()
    db.commit()
    db.refresh(record)

    return _enrich_pipeline_record(record, db)


@router.get("/", response_model=List[PipelineResponse])
def list_pipeline(
    status_filter: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns the live pipeline roster for the dashboard."""
    _require_admin_or_hr(current_user)

    query = db.query(OnboardingPipeline)
    if status_filter:
        query = query.filter(OnboardingPipeline.status == status_filter)
    query = query.order_by(OnboardingPipeline.created_at.desc())

    records = query.all()
    return [_enrich_pipeline_record(r, db) for r in records]


@router.get("/my-status")
def my_pipeline_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Candidate checks if they have a pending confirmation."""
    record = db.query(OnboardingPipeline).filter(
        OnboardingPipeline.candidate_id == current_user.id,
        OnboardingPipeline.status == "pending_confirmation",
    ).first()

    if not record:
        return {"has_pending": False}

    return {
        "has_pending": True,
        "pipeline": _enrich_pipeline_record(record, db),
    }


@router.get("/my-mentees", response_model=List[PipelineResponse])
def my_mentees(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """TL/Buddy fetches candidates currently assigned to them in the pipeline."""
    if not current_user.employee_id:
        return []

    records = db.query(OnboardingPipeline).filter(
        OnboardingPipeline.buddy_id == current_user.employee_id,
        OnboardingPipeline.status.in_(["in_progress", "day_5_pending"])
    ).order_by(OnboardingPipeline.created_at.desc()).all()

    return [_enrich_pipeline_record(r, db) for r in records]


@router.post("/{pipeline_id}/evaluate")
def evaluate_candidate(
    pipeline_id: int,
    payload: PipelineEvaluateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Buddy/PM submits the Day-5 evaluation. On 'passed', auto-creates an Allocation."""
    record = db.query(OnboardingPipeline).filter(
        OnboardingPipeline.id == pipeline_id,
    ).first()

    if not record:
        raise HTTPException(status_code=404, detail="Pipeline record not found.")

    is_admin = current_user.role in ("admin", "hr", "pm")
    is_buddy = (current_user.employee_id is not None) and (record.buddy_id == current_user.employee_id)

    if not (is_admin or is_buddy):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin, HR, PM, or the assigned buddy can evaluate this candidate.",
        )

    if record.status not in ("day_5_pending", "in_progress"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot evaluate a pipeline with status '{record.status}'.",
        )

    # Update evaluation fields
    record.eval_score = payload.score
    record.eval_notes = payload.notes
    record.evaluated_at = datetime.utcnow()
    record.status = payload.result  # 'passed' or 'failed'

    # If passed, auto-create an Allocation record
    candidate = db.query(User).filter(User.id == record.candidate_id).first()
    project = db.query(ParentProject).filter(ParentProject.id == record.project_id).first()
    project_name = project.name if project else "General Onboarding"
    
    if payload.result == "passed" and record.project_id:
        # Find the candidate's employee_id
        if candidate and candidate.employee_id:
            allocation = Allocation(
                employee_id=candidate.employee_id,
                sub_project_id=None,  # Will be linked when specific sub-project is chosen
                active_start_date=date.today(),
                role_tags=["Annotator / Reviewer"],
                is_active=True,
            )
            db.add(allocation)

    db.commit()
    db.refresh(record)

    # Send email notification
    if candidate and candidate.email:
        if payload.result == "passed":
            try_send_evaluation_passed_email(
                to_email=candidate.email, 
                to_name=candidate.name, 
                project_name=project_name
            )
        elif payload.result == "failed":
            try_send_evaluation_failed_email(
                to_email=candidate.email, 
                to_name=candidate.name, 
                project_name=project_name
            )

    return _enrich_pipeline_record(record, db)
