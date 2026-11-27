from datetime import datetime, timezone
import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session, joinedload, defer
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, case, exists, or_

from app.db.database import get_db
from app.models.user import User
from app.models.employee import Employee
from app.models.cluster import Cluster
from app.models.assignment import Assignment
from app.models.assessment import Assessment, AssessmentAttempt, AssessmentEnrollment
from app.schemas.assignment import (
    AssignmentCreate,
    AssignmentDeadlineUpdate,
    AssignmentResponse,
)
from app.services import audit_service
from app.services.auth_service import require_assessment_admin
from app.services.cluster_service import (
    fan_out_cluster,
    deactivate_assignment as svc_deactivate_assignment,
)
from app.services.assessment_lifecycle import utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/assignments", tags=["Assignments"])


def _attempted_enrollment_expr():
    has_finished_attempt = exists().where(
        AssessmentAttempt.enrollment_id == AssessmentEnrollment.id,
        AssessmentAttempt.status.in_(["completed", "abandoned"]),
    )
    return or_(
        AssessmentEnrollment.status.in_(["passed", "failed"]),
        has_finished_attempt,
    )


def _assignment_counts(db: Session, assignment_id: int) -> tuple[int, int, int, int]:
    attempted_expr = _attempted_enrollment_expr()
    row = (
        db.query(
            func.count(AssessmentEnrollment.id),
            func.coalesce(func.sum(case((attempted_expr, 1), else_=0)), 0),
            func.coalesce(func.sum(case((AssessmentEnrollment.status == "passed", 1), else_=0)), 0),
            func.coalesce(func.sum(case((AssessmentEnrollment.status == "in_progress", 1), else_=0)), 0),
        )
        .join(Employee, Employee.id == AssessmentEnrollment.employee_id)
        .filter(
            AssessmentEnrollment.assignment_id == assignment_id,
            AssessmentEnrollment.status != "cancelled",
            Employee.status.in_(["active", "on-leave"]),
        )
        .first()
    )
    if not row:
        return (0, 0, 0, 0)
    return (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0), int(row[3] or 0))


@router.post("", response_model=AssignmentResponse, status_code=status.HTTP_201_CREATED)
def create_assignment(
    payload: AssignmentCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """
    Admin/HR only: Creates a new cohort assignment for a published assessment and
    atomically fans out enrollments to all active roster members in one transaction.
    """
    cluster = db.query(Cluster).filter_by(id=payload.cluster_id, is_archived=False).first()
    if not cluster:
        raise HTTPException(status_code=404, detail="Target cluster not found or archived.")

    now = datetime.now(timezone.utc)
    normalized_deadline = utc(payload.deadline) if payload.deadline is not None else None
    if normalized_deadline is not None and normalized_deadline <= now:
        raise HTTPException(status_code=400, detail="Deadline must be a future datetime.")

    assessment = (
        db.query(Assessment)
        .options(defer(Assessment.test_config))
        .filter_by(id=payload.assessment_id)
        .first()
    )
    if not assessment:
        raise HTTPException(status_code=404, detail="Assessment not found.")
    if assessment.status != "published":
        raise HTTPException(
            status_code=400,
            detail="Only published assessments can be assigned to cohorts.",
        )

    existing = (
        db.query(Assignment)
        .filter_by(
            cluster_id=payload.cluster_id,
            assessment_id=payload.assessment_id,
            is_active=True,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail="This assessment is already actively assigned to this cluster.",
        )

    assignment = Assignment(
        cluster_id=payload.cluster_id,
        assessment_id=payload.assessment_id,
        deadline=normalized_deadline,
        relative_due_days=payload.relative_due_days,
        is_active=True,
        created_by=current_user.id,
    )
    db.add(assignment)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This assessment is already actively assigned to this cluster.",
        )

    fan_out_cluster(db, cluster, now=now)

    audit_service.record(
        db,
        actor=current_user,
        action="assignment.created",
        category="Assessments",
        action_type="Created",
        entity_type="assignment",
        entity_id=assignment.id,
        entity_name=f"{cluster.name} -> {assessment.title}",
        summary=f"Assigned '{assessment.title}' to cluster '{cluster.name}'",
        request=request,
    )

    db.commit()
    db.refresh(assignment)

    enrolled_count, completed_count, passed_count, in_progress_count = _assignment_counts(db, assignment.id)
    resp = AssignmentResponse.model_validate(assignment)
    resp.cluster_name = cluster.name
    resp.assessment_title = assessment.title
    resp.total_enrolled = enrolled_count
    resp.completed_count = completed_count
    resp.passed_count = passed_count
    resp.in_progress_count = in_progress_count
    return resp


@router.get("", response_model=List[AssignmentResponse])
def list_assignments(
    cluster_id: Optional[int] = Query(None),
    assignment_type: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(True),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """Admin/HR only: Lists assignments with completion analytics, excluding cancelled/archived/inactive."""
    if assignment_type and assignment_type != "assessment":
        return []

    query = db.query(Assignment).options(
        joinedload(Assignment.cluster),
        joinedload(Assignment.assessment).defer(Assessment.test_config),
    )
    if cluster_id:
        query = query.filter(Assignment.cluster_id == cluster_id)
    if assignment_type == "assessment":
        query = query.filter(Assignment.assessment_id.isnot(None))
    if is_active is not None:
        query = query.filter(Assignment.is_active == is_active)

    assignments = (
        query.order_by(Assignment.created_at.desc(), Assignment.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    if not assignments:
        return []

    asg_ids = [a.id for a in assignments]
    attempted_expr = _attempted_enrollment_expr()
    count_rows = (
        db.query(
            AssessmentEnrollment.assignment_id,
            func.count(AssessmentEnrollment.id),
            func.coalesce(func.sum(case((attempted_expr, 1), else_=0)), 0),
            func.coalesce(func.sum(case((AssessmentEnrollment.status == "passed", 1), else_=0)), 0),
            func.coalesce(func.sum(case((AssessmentEnrollment.status == "in_progress", 1), else_=0)), 0),
        )
        .join(Employee, Employee.id == AssessmentEnrollment.employee_id)
        .filter(
            AssessmentEnrollment.assignment_id.in_(asg_ids),
            AssessmentEnrollment.status != "cancelled",
            Employee.status.in_(["active", "on-leave"]),
        )
        .group_by(AssessmentEnrollment.assignment_id)
        .all()
    )
    enrolled_map = {aid: int(total or 0) for aid, total, _, _, _ in count_rows}
    completed_map = {aid: int(comp or 0) for aid, _, comp, _, _ in count_rows}
    passed_map = {aid: int(pas or 0) for aid, _, _, pas, _ in count_rows}
    in_progress_map = {aid: int(ip or 0) for aid, _, _, _, ip in count_rows}

    results = []
    for a in assignments:
        resp = AssignmentResponse.model_validate(a)
        if a.cluster:
            resp.cluster_name = a.cluster.name
        if a.assessment:
            resp.assessment_title = a.assessment.title
        if a.assessment_id:
            resp.total_enrolled = enrolled_map.get(a.id, 0)
            resp.completed_count = completed_map.get(a.id, 0)
            resp.passed_count = passed_map.get(a.id, 0)
            resp.in_progress_count = in_progress_map.get(a.id, 0)
        results.append(resp)

    return results


@router.post("/{assignment_id}/deactivate", status_code=status.HTTP_200_OK)
def deactivate_assignment(
    assignment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """Admin/HR only: Soft-deactivates an assignment and deduplicates candidate enrollments."""
    assignment = (
        db.query(Assignment)
        .options(
            joinedload(Assignment.cluster),
            joinedload(Assignment.assessment).defer(Assessment.test_config),
        )
        .filter_by(id=assignment_id)
        .first()
    )
    if not assignment or not assignment.is_active:
        raise HTTPException(status_code=404, detail="Assignment not found or already inactive.")

    svc_deactivate_assignment(db, assignment_id)

    audit_service.record(
        db,
        actor=current_user,
        action="assignment.deactivated",
        category="Assessments",
        action_type="Updated",
        entity_type="assignment",
        entity_id=assignment.id,
        entity_name=(
            f"{assignment.cluster.name if assignment.cluster else assignment.cluster_id} -> "
            f"{assignment.assessment.title if assignment.assessment else assignment.assessment_id}"
        ),
        summary=f"Deactivated assignment #{assignment.id}",
        request=request,
    )

    db.commit()
    return {"detail": "Assignment deactivated successfully."}


@router.put("/{assignment_id}/deadline", response_model=AssignmentResponse)
def update_assignment_deadline(
    assignment_id: int,
    payload: AssignmentDeadlineUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """
    Admin/HR only: Cohort deadline extension implementing the 'Never Shorten, Auto-Clear Redundant' rule.
    Only valid for active fixed-deadline assignments.
    """
    assignment = (
        db.query(Assignment)
        .options(
            joinedload(Assignment.cluster),
            joinedload(Assignment.assessment).defer(Assessment.test_config),
        )
        .filter_by(id=assignment_id)
        .first()
    )
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    if not assignment.is_active:
        raise HTTPException(status_code=400, detail="Cannot extend deadline of an inactive assignment.")

    if assignment.relative_due_days is not None:
        raise HTTPException(
            status_code=400,
            detail="Relative assignments compute due date dynamically per member join and do not support baseline deadline updates.",
        )

    now = datetime.now(timezone.utc)
    new_dl = utc(payload.new_deadline)
    if not new_dl or new_dl <= now:
        raise HTTPException(status_code=400, detail="New deadline must be a future datetime.")

    old_dl = utc(assignment.deadline)
    if old_dl and new_dl <= old_dl:
        raise HTTPException(
            status_code=400,
            detail="New cohort deadline must be later than the current deadline (Never Shorten rule).",
        )

    assignment.deadline = new_dl
    assignment.updated_at = now

    # Auto-clear candidate overrides that are now redundant (<= new_cohort_deadline)
    db.query(AssessmentEnrollment).filter(
        AssessmentEnrollment.assignment_id == assignment.id,
        AssessmentEnrollment.deadline.isnot(None),
        AssessmentEnrollment.deadline <= new_dl,
    ).update({"deadline": None, "updated_at": now}, synchronize_session=False)

    # Re-run fan out if assignment was expired so members who joined while expired get enrolled
    if old_dl and old_dl <= now and assignment.cluster:
        fan_out_cluster(db, assignment.cluster, now=now)

    audit_service.record(
        db,
        actor=current_user,
        action="assignment.deadline_extended",
        category="Assessments",
        action_type="Updated",
        entity_type="assignment",
        entity_id=assignment.id,
        entity_name=(
            f"{assignment.cluster.name if assignment.cluster else assignment.cluster_id} -> "
            f"{assignment.assessment.title if assignment.assessment else assignment.assessment_id}"
        ),
        details=audit_service.changes(
            audit_service.field_diff("Cohort deadline", old_dl, new_dl),
        ),
        summary=f"Extended cohort assignment #{assignment.id} deadline from {old_dl} to {new_dl}",
        request=request,
    )

    db.commit()
    db.refresh(assignment)

    total_enrolled, completed_count, passed_count, in_progress_count = _assignment_counts(db, assignment.id)
    resp = AssignmentResponse.model_validate(assignment)
    if assignment.cluster:
        resp.cluster_name = assignment.cluster.name
    if assignment.assessment:
        resp.assessment_title = assignment.assessment.title
    resp.total_enrolled = total_enrolled
    resp.completed_count = completed_count
    resp.passed_count = passed_count
    resp.in_progress_count = in_progress_count
    return resp
