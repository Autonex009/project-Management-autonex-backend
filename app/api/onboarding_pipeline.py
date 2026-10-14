"""
Onboarding Pipeline API — manages the 5-day shadow/training workflow.

Endpoints:
  POST /api/onboarding/pipeline/assign    – PM assigns candidate to project + buddy
  POST /api/onboarding/pipeline/confirm   – Candidate accepts the assignment
  GET  /api/onboarding/pipeline           – Dashboard roster (all pipeline records)
  POST /api/onboarding/pipeline/{id}/evaluate – Buddy submits Day-5 evaluation
"""
import time
import logging
from datetime import datetime, date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.db.database import get_db
from app.models.onboarding_pipeline import OnboardingPipeline
from app.models.user import User
from app.models.employee import Employee
from app.models.parent_project import ParentProject
from app.models.project import DailySheet as SubProject
from app.models.allocation import Allocation
from app.models.signup_request import SignupRequest
from app.schemas.onboarding_pipeline import (
    PipelineAssignRequest,
    PipelineBulkAssignRequest,
    PipelineEvaluateRequest,
    PipelineResponse,
)
from app.services.auth_service import get_current_user
from app.utils.business_time import calculate_business_days
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

_CACHED_TRAINING_SHEET_ID: Optional[int] = None
_CACHED_TRAINING_SHEET_TIME: float = 0
_CACHE_TTL_SECONDS: float = 300.0  # 5 minutes


def clear_training_sheet_cache() -> None:
    """Explicitly invalidate the cached training sheet ID."""
    global _CACHED_TRAINING_SHEET_ID, _CACHED_TRAINING_SHEET_TIME
    _CACHED_TRAINING_SHEET_ID = None
    _CACHED_TRAINING_SHEET_TIME = 0


def _get_active_training_sheet_id(db: Session) -> Optional[int]:
    """Dynamically resolve the active Training daily sheet ID with in-memory TTL caching."""
    global _CACHED_TRAINING_SHEET_ID, _CACHED_TRAINING_SHEET_TIME
    now = time.time()
    if _CACHED_TRAINING_SHEET_ID and (now - _CACHED_TRAINING_SHEET_TIME < _CACHE_TTL_SECONDS):
        return _CACHED_TRAINING_SHEET_ID

    sheet_id = (
        db.query(SubProject.id)
        .filter(
            SubProject.project_type.ilike("training"),
            SubProject.project_status != "archived",
        )
        .scalar()
        or db.query(SubProject.id)
        .filter(
            SubProject.name.ilike("%Week 1 training%"),
            SubProject.project_status != "archived",
        )
        .scalar()
    )
    if sheet_id:
        _CACHED_TRAINING_SHEET_ID = sheet_id
        _CACHED_TRAINING_SHEET_TIME = now
        return sheet_id
    return None


def _determine_trainee_role_tag(designation: Optional[str]) -> list[str]:
    """Annotators/Reviewers/Interns receive ['Trainee']; Leadership keeps their title."""
    if not designation:
        return ["Trainee"]
    desig_lower = designation.strip().lower()
    is_leadership = any(role in desig_lower for role in ["team lead", "lead", "manager", "admin", "hr", "pm"])
    if is_leadership:
        return ["Team Lead"] if "lead" in desig_lower else ["Annotator / Reviewer"]
    return ["Trainee"]


def background_backfill_training_allocations(training_sheet_id: int) -> None:
    """
    Non-blocking background worker that automatically allocates active pipeline candidates
    who were assigned before a training project existed.
    Optimized for Supabase: uses 2 indexed queries and bulk inserts in 1 flush.
    """
    from app.db.database import SessionLocal
    from app.api.allocations import sync_project_allocations

    db = SessionLocal()
    try:
        # 1. Fetch active pipeline candidates lacking training allocations
        active_candidates = (
            db.query(User.employee_id, Employee.designation)
            .join(OnboardingPipeline, OnboardingPipeline.candidate_id == User.id)
            .join(Employee, User.employee_id == Employee.id)
            .filter(
                OnboardingPipeline.status.in_(["pending_confirmation", "in_progress", "day_5_pending"]),
                User.employee_id.isnot(None),
                Employee.status == "active",
            )
            .all()
        )
        emp_ids = list({row[0] for row in active_candidates if row[0]})
        if not emp_ids:
            return

        # 2. Check existing active allocations on this training project
        already_allocated = {
            row[0]
            for row in db.query(Allocation.employee_id)
            .filter(
                Allocation.employee_id.in_(emp_ids),
                Allocation.sub_project_id == training_sheet_id,
                Allocation.is_active == True,
            )
            .all()
        }

        # 3. Bulk construct missing allocations
        allocations_to_add = []
        seen_emp_ids = set()
        for emp_id, designation in active_candidates:
            if emp_id not in already_allocated and emp_id not in seen_emp_ids:
                seen_emp_ids.add(emp_id)
                allocations_to_add.append(
                    Allocation(
                        employee_id=emp_id,
                        sub_project_id=training_sheet_id,
                        active_start_date=date.today(),
                        role_tags=_determine_trainee_role_tag(designation),
                        is_active=True,
                        total_daily_hours=8,
                    )
                )

        if allocations_to_add:
            db.add_all(allocations_to_add)
            db.flush()
            sync_project_allocations(db, training_sheet_id)
            db.commit()
            logger.info(
                "Background worker backfilled %d active trainees onto Training Sheet #%d.",
                len(allocations_to_add),
                training_sheet_id,
            )
    except Exception as exc:
        db.rollback()
        logger.error("Failed to backfill training allocations: %s", exc)
    finally:
        db.close()


def _enrich_pipeline_records_bulk(records: List[OnboardingPipeline], db: Session) -> List[PipelineResponse]:
    """Batch-enrich pipeline records in 5 queries total rather than 5N sequential queries."""
    if not records:
        return []

    cand_ids = list({r.candidate_id for r in records})
    proj_ids = list({r.project_id for r in records if r.project_id})
    sub_proj_ids = list({r.sub_project_id for r in records if r.sub_project_id})
    buddy_ids = list({r.buddy_id for r in records if r.buddy_id})

    user_map = {
        u.id: u for u in db.query(User.id, User.name, User.email).filter(User.id.in_(cand_ids)).all()
    } if cand_ids else {}

    proj_map = {
        p.id: p.name for p in db.query(ParentProject.id, ParentProject.name).filter(ParentProject.id.in_(proj_ids)).all()
    } if proj_ids else {}

    sub_proj_map = {
        sp.id: sp.name for sp in db.query(SubProject.id, SubProject.name).filter(SubProject.id.in_(sub_proj_ids)).all()
    } if sub_proj_ids else {}

    buddy_map = {
        b.id: b.name for b in db.query(Employee.id, Employee.name).filter(Employee.id.in_(buddy_ids)).all()
    } if buddy_ids else {}

    cand_emails = [u.email for u in user_map.values() if u.email]
    signup_map = {
        s.email: s for s in db.query(SignupRequest.email, SignupRequest.created_at, SignupRequest.reviewed_at).filter(SignupRequest.email.in_(cand_emails)).all()
    } if cand_emails else {}

    now = datetime.utcnow()
    responses = []
    for r in records:
        user = user_map.get(r.candidate_id)
        signup = signup_map.get(user.email) if user and user.email else None
        days_elapsed = (now - r.started_at).days if r.started_at else None

        responses.append(PipelineResponse(
            id=r.id,
            candidate_id=r.candidate_id,
            candidate_name=user.name if user else None,
            candidate_email=user.email if user else None,
            project_id=r.project_id,
            project_name=proj_map.get(r.project_id),
            sub_project_id=r.sub_project_id,
            sub_project_name=sub_proj_map.get(r.sub_project_id),
            buddy_id=r.buddy_id,
            buddy_name=buddy_map.get(r.buddy_id),
            status=r.status,
            days_elapsed=days_elapsed,
            started_at=r.started_at,
            expected_eval_date=r.expected_eval_date,
            applied_at=signup.created_at if signup else None,
            approved_at=signup.reviewed_at if signup else None,
            evaluated_at=r.evaluated_at,
            eval_score=r.eval_score,
            eval_notes=r.eval_notes,
            created_at=r.created_at,
        ))
    return responses

def _enrich_pipeline_record(record: OnboardingPipeline, db: Session) -> PipelineResponse:
    """Join in candidate name, project name, buddy name, and compute days_elapsed."""
    candidate = db.query(User).filter(User.id == record.candidate_id).first()
    project = db.query(ParentProject).filter(ParentProject.id == record.project_id).first() if record.project_id else None
    
    sub_project_name = None
    if record.sub_project_id:
        sp = db.query(SubProject).filter(SubProject.id == record.sub_project_id).first()
        sub_project_name = sp.name if sp else None

    buddy = db.query(Employee).filter(Employee.id == record.buddy_id).first() if record.buddy_id else None

    days_elapsed = None
    if record.started_at:
        delta = datetime.utcnow() - record.started_at
        days_elapsed = delta.days

    signup = None
    if candidate and candidate.email:
        signup = db.query(SignupRequest).filter(SignupRequest.email == candidate.email).first()

    return PipelineResponse(
        id=record.id,
        candidate_id=record.candidate_id,
        candidate_name=candidate.name if candidate else None,
        candidate_email=candidate.email if candidate else None,
        project_id=record.project_id,
        project_name=project.name if project else None,
        sub_project_id=record.sub_project_id,
        sub_project_name=sub_project_name,
        buddy_id=record.buddy_id,
        buddy_name=buddy.name if buddy else None,
        status=record.status,
        days_elapsed=days_elapsed,
        started_at=record.started_at,
        expected_eval_date=record.expected_eval_date,
        applied_at=signup.created_at if signup else None,
        approved_at=signup.reviewed_at if signup else None,
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
    """PM/HR assigns a candidate to a project and buddy. Creates a pipeline record and auto-allocates to training."""
    _require_admin_or_hr(current_user)

    from app.api.allocations import sync_project_allocations

    # Validate candidate exists
    candidate = db.query(User).filter(User.id == payload.candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found.")

    # Check for duplicate active pipeline using composite index
    existing_status = db.query(OnboardingPipeline.status).filter(
        OnboardingPipeline.candidate_id == payload.candidate_id,
        OnboardingPipeline.status.in_(["pending_confirmation", "in_progress", "day_5_pending"]),
    ).scalar()
    if existing_status:
        raise HTTPException(
            status_code=409,
            detail=f"Candidate already has an active pipeline (status: {existing_status}).",
        )

    # Validate project exists
    if payload.project_id:
        if not db.query(ParentProject.id).filter(ParentProject.id == payload.project_id).scalar():
            raise HTTPException(status_code=404, detail="Project not found.")
            
    if payload.sub_project_id:
        if not db.query(SubProject.id).filter(SubProject.id == payload.sub_project_id).scalar():
            raise HTTPException(status_code=404, detail="Sub-Project not found.")

    # Validate buddy exists
    if not db.query(Employee.id).filter(Employee.id == payload.buddy_id).scalar():
        raise HTTPException(status_code=404, detail="Buddy/Mentor not found.")

    record = OnboardingPipeline(
        candidate_id=payload.candidate_id,
        project_id=payload.project_id,
        sub_project_id=payload.sub_project_id,
        buddy_id=payload.buddy_id,
        status="pending_confirmation",
    )
    db.add(record)

    # Auto-allocate to training daily sheet
    training_sheet_id = _get_active_training_sheet_id(db)
    if not training_sheet_id:
        logger.warning(
            "No active Training daily sheet found in database; candidate #%s added to pipeline without training allocation.",
            candidate.id
        )
    elif not candidate.employee_id:
        logger.warning(
            "Candidate user #%s has no linked employee_id; skipping training allocation.",
            candidate.id
        )
    else:
        has_alloc = db.query(Allocation.id).filter(
            Allocation.employee_id == candidate.employee_id,
            Allocation.sub_project_id == training_sheet_id,
            Allocation.is_active == True,
        ).scalar()
        if not has_alloc:
            emp_desig = db.query(Employee.designation).filter(Employee.id == candidate.employee_id).scalar()
            alloc = Allocation(
                employee_id=candidate.employee_id,
                sub_project_id=training_sheet_id,
                active_start_date=date.today(),
                role_tags=_determine_trainee_role_tag(emp_desig),
                is_active=True,
                total_daily_hours=8,
            )
            db.add(alloc)
            db.flush()
            sync_project_allocations(db, training_sheet_id)

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

    from app.api.allocations import sync_project_allocations

    # 1. Scalar validations
    if payload.project_id and not db.query(ParentProject.id).filter(ParentProject.id == payload.project_id).scalar():
        raise HTTPException(status_code=404, detail="Project not found.")
    if payload.sub_project_id and not db.query(SubProject.id).filter(SubProject.id == payload.sub_project_id).scalar():
        raise HTTPException(status_code=404, detail="Sub-Project not found.")
    if payload.buddy_id and not db.query(Employee.id).filter(Employee.id == payload.buddy_id).scalar():
        raise HTTPException(status_code=404, detail="Buddy/Mentor not found.")

    # 2. Bulk fetch candidates with employee designation in 1 query
    cand_rows = (
        db.query(User.id, User.employee_id, Employee.designation)
        .outerjoin(Employee, User.employee_id == Employee.id)
        .filter(User.id.in_(payload.candidate_ids))
        .all()
    )
    found_ids = {row[0] for row in cand_rows}
    missing_ids = set(payload.candidate_ids) - found_ids
    if missing_ids:
        raise HTTPException(status_code=404, detail=f"Candidates not found: {missing_ids}")

    # 3. Check existing active pipelines in 1 query
    existing = db.query(OnboardingPipeline.candidate_id).filter(
        OnboardingPipeline.candidate_id.in_(payload.candidate_ids),
        OnboardingPipeline.status.in_(["pending_confirmation", "in_progress", "day_5_pending"]),
    ).all()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Candidates {[e[0] for e in existing]} already have an active pipeline.",
        )

    # 4. Resolve training sheet ONCE
    training_sheet_id = _get_active_training_sheet_id(db)

    emp_ids = [row[1] for row in cand_rows if row[1]]
    existing_allocated_emp_ids = {
        row[0]
        for row in db.query(Allocation.employee_id).filter(
            Allocation.employee_id.in_(emp_ids),
            Allocation.sub_project_id == training_sheet_id,
            Allocation.is_active == True,
        ).all()
    } if emp_ids and training_sheet_id else set()

    # 5. Build records
    records = []
    allocations_to_add = []
    for uid, emp_id, designation in cand_rows:
        records.append(OnboardingPipeline(
            candidate_id=uid,
            project_id=payload.project_id,
            sub_project_id=payload.sub_project_id,
            buddy_id=payload.buddy_id,
            status="pending_confirmation",
        ))
        if training_sheet_id and emp_id and emp_id not in existing_allocated_emp_ids:
            allocations_to_add.append(Allocation(
                employee_id=emp_id,
                sub_project_id=training_sheet_id,
                active_start_date=date.today(),
                role_tags=_determine_trainee_role_tag(designation),
                is_active=True,
                total_daily_hours=8,
            ))

    db.add_all(records)
    if allocations_to_add:
        db.add_all(allocations_to_add)
        db.flush()
        sync_project_allocations(db, training_sheet_id)

    db.commit()

    return _enrich_pipeline_records_bulk(records, db)


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
    now = datetime.utcnow()
    record.started_at = now
    record.expected_eval_date = calculate_business_days(now.date(), 5)
    db.commit()
    db.refresh(record)

    return _enrich_pipeline_record(record, db)


@router.get("", response_model=List[PipelineResponse])
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
    return _enrich_pipeline_records_bulk(records, db)


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
        OnboardingPipeline.status.in_(["pending_confirmation", "in_progress", "day_5_pending"])
    ).order_by(OnboardingPipeline.created_at.desc()).all()

    return _enrich_pipeline_records_bulk(records, db)


@router.post("/{pipeline_id}/evaluate")
def evaluate_candidate(
    pipeline_id: int,
    payload: PipelineEvaluateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Buddy/PM submits Day-5 evaluation. Unallocates from training and auto-allocates to target project on pass."""
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

    candidate = db.query(User).filter(User.id == record.candidate_id).first()
    
    project_name = "General Onboarding"
    if record.sub_project_id:
        sp = db.query(SubProject).filter(SubProject.id == record.sub_project_id).first()
        if sp:
            project_name = sp.name
    elif record.project_id:
        project = db.query(ParentProject).filter(ParentProject.id == record.project_id).first()
        if project:
            project_name = project.name

    from app.api.allocations import sync_project_allocations
    training_sheet_id = _get_active_training_sheet_id(db)

    # 1. Unallocate from Training project on BOTH pass and fail
    if candidate and candidate.employee_id:
        training_allocs = []
        if training_sheet_id:
            training_allocs = db.query(Allocation).filter(
                Allocation.employee_id == candidate.employee_id,
                Allocation.sub_project_id == training_sheet_id,
                Allocation.is_active == True,
            ).all()

        # Fallback: deallocate any active allocation on any daily_sheet marked as training
        if not training_allocs:
            training_allocs = (
                db.query(Allocation)
                .join(SubProject, Allocation.sub_project_id == SubProject.id)
                .filter(
                    Allocation.employee_id == candidate.employee_id,
                    Allocation.is_active == True,
                    (SubProject.project_type.ilike("training") | SubProject.name.ilike("%Week 1 training%")),
                )
                .all()
            )

        for ta in training_allocs:
            ta.is_active = False
            ta.active_end_date = date.today()
            ta.deactivated_reason = f"Completed onboarding evaluation ({payload.result})"
        if training_allocs:
            db.flush()
            for sp_id in {ta.sub_project_id for ta in training_allocs}:
                sync_project_allocations(db, sp_id)

        # 2. On PASSED: Create Target Project allocation (idempotent, no duplicates)
        if payload.result == "passed" and (record.sub_project_id or record.project_id):
            sub_proj_id = record.sub_project_id
            if not sub_proj_id and record.project_id:
                first_sub_project = db.query(SubProject.id).filter(
                    SubProject.main_project_id == record.project_id,
                    SubProject.project_status != "archived",
                ).first()
                if first_sub_project:
                    sub_proj_id = first_sub_project[0]

            if sub_proj_id:
                has_existing_alloc = db.query(Allocation.id).filter(
                    Allocation.employee_id == candidate.employee_id,
                    Allocation.sub_project_id == sub_proj_id,
                    Allocation.is_active == True,
                ).scalar()
                if not has_existing_alloc:
                    emp_desig = db.query(Employee.designation).filter(Employee.id == candidate.employee_id).scalar()
                    target_role_tag = ["Team Lead"] if emp_desig and "lead" in emp_desig.lower() else ["Annotator / Reviewer"]
                    allocation = Allocation(
                        employee_id=candidate.employee_id,
                        sub_project_id=sub_proj_id,
                        active_start_date=date.today(),
                        role_tags=target_role_tag,
                        is_active=True,
                        total_daily_hours=8,
                    )
                    db.add(allocation)
                    db.flush()
                    sync_project_allocations(db, sub_proj_id)

        # 3. On FAILED: Archive employee and deactivate user
        elif payload.result == "failed":
            candidate.is_active = False
            emp = db.query(Employee).filter(Employee.id == candidate.employee_id).first()
            if emp:
                emp.status = "archived"

    db.commit()
    db.refresh(record)

    # 4. Defer email dispatch to background task (Pass primitives only, zero DB connection hold time!)
    if candidate and candidate.email:
        cand_email = str(candidate.email)
        cand_name = str(candidate.name)
        if payload.result == "passed":
            background_tasks.add_task(
                try_send_evaluation_passed_email,
                to_email=cand_email, 
                to_name=cand_name, 
                project_name=project_name,
            )
        elif payload.result == "failed":
            background_tasks.add_task(
                try_send_evaluation_failed_email,
                to_email=cand_email, 
                to_name=cand_name, 
                project_name=project_name,
            )

    return _enrich_pipeline_record(record, db)
