from datetime import datetime, timezone, timedelta
import logging
import os
import tempfile
import threading
import time
from typing import List, Optional, Union
import anyio
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, joinedload, selectinload, defer, load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy import func, or_, and_, case, exists, text

from app.db.database import get_db
from app.models.user import User
from app.models.employee import Employee
from app.models.notification import Notification
from app.models.assignment import Assignment
from app.models.cluster import Cluster
from app.models.assessment import (
    Assessment,
    AssessmentEnrollment,
    AssessmentAttempt,
    AssessmentLog,
    AssessmentRecording,
)
from app.schemas.assessment import (
    AssessmentCreate,
    AssessmentUpdate,
    AssessmentRestore,
    AssessmentOverviewResponse,
    AssessmentListResponse,
    AssessmentDetailResponse,
    StartAttemptRequest,
    TelemetryIngestRequest,
    DraftAutosaveRequest,
    SubmitAttemptRequest,
    GrantRetestRequest,
    CandidateDeadlineExtendRequest,
    CandidateAttemptResponse,
    CandidateAttemptResultResponse,
    AttemptResultResponse,
    CandidateScorecardResponse,
    ScorecardResponse,
    AssessmentLogResponse,
    ManualReminderRequest,
)
from app.services.auth_service import (
    get_current_user,
    require_assessment_admin,
    require_assessment_viewer,
)
from app.services import audit_service, project_scope
from app.services.assessment_service import (
    validate_blueprint_for_publish,
    generate_candidate_exam_paper,
    sanitize_snapshot_for_candidate,
    sanitize_result_summary_for_candidate,
    validate_draft_payload,
    AssessmentValidationError,
    utc,
    effective_allowed_attempts,
    finalize_attempt,
)
from app.services.assessment_storage_service import (
    create_signed_upload_url,
    create_signed_play_url,
    check_recording_exists,
    is_local_fallback_allowed,
    LOCAL_RECORDINGS_DIR,
)
from app.services.cluster_service import (
    archive_assessment_cascade,
    find_alternate_active_assignment,
    reactivate_assignments_for_assessment,
)
from app.services.document_storage_service import is_supabase_configured
from app.services.slack_service import try_send_assessment_reminder_dm, SlackRateLimitError
from app.utils.business_time import IST

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/assessments", tags=["Assessments"])

MAX_LOCAL_RECORDING_BYTES = 250 * 1024 * 1024  # 250 MB safety limit for local dev fallback
MAX_TELEMETRY_LOGS_PER_ATTEMPT = 1000
REMINDER_COOLDOWN_MINUTES = 60
POSTGRES_LOCK_TIMEOUT_MS = 2000
VALID_TELEMETRY_EVENTS = {
    "tab_switch",
    "window_blur",
    "fullscreen_exit",
    "camera_lost",
    "multiple_faces",
    "no_face",
}


def _set_lock_timeout(db: Session, timeout_ms: int = POSTGRES_LOCK_TIMEOUT_MS) -> None:
    """Bounds PostgreSQL row-lock waits so contention fails fast instead of exhausting the connection pool."""
    bind = db.get_bind() if hasattr(db, "get_bind") else getattr(db, "bind", None)
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(text(f"SET LOCAL lock_timeout = '{int(timeout_ms)}ms'"))


def _is_lock_contention_error(exc: OperationalError) -> bool:
    orig = getattr(exc, "orig", None)
    pgcode = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    if pgcode == "55P03":
        return True
    msg = str(orig or exc).lower()
    return (
        "lock timeout" in msg
        or "could not obtain lock" in msg
        or "canceling statement due to lock timeout" in msg
    )


def _lock_conflict_http_exception(
    detail: str = "Resource is currently locked by another request; please retry shortly.",
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=detail,
        headers={"Retry-After": "1"},
    )


def _effective_deadline(enr: AssessmentEnrollment) -> Optional[datetime]:
    return utc(enr.deadline) or (utc(enr.assignment.deadline) if enr.assignment else None)


def _fetch_attempt_event_counts(
    db: Session,
    attempt_ids: List[int],
) -> dict[int, dict[str, int]]:
    """Single grouped COUNT(*) aggregation for proctoring log counters across N attempts."""
    if not attempt_ids:
        return {}
    rows = (
        db.query(AssessmentLog.attempt_id, AssessmentLog.event_type, func.count(AssessmentLog.id))
        .filter(AssessmentLog.attempt_id.in_(attempt_ids))
        .group_by(AssessmentLog.attempt_id, AssessmentLog.event_type)
        .all()
    )
    counts: dict[int, dict[str, int]] = {}
    for att_id, event_type, cnt in rows:
        counts.setdefault(int(att_id), {})[str(event_type)] = int(cnt or 0)
    return counts


def _fetch_recording_statuses(db: Session, attempt_ids: List[int]) -> dict[int, str]:
    """One indexed lookup of proctoring-video status for N attempts (no per-row queries)."""
    if not attempt_ids:
        return {}
    rows = (
        db.query(AssessmentRecording.attempt_id, AssessmentRecording.status)
        .filter(AssessmentRecording.attempt_id.in_(attempt_ids))
        .all()
    )
    return {int(att_id): str(st) for att_id, st in rows}


def _fetch_attempt_logs(db: Session, attempt_ids: List[int]) -> dict[int, list]:
    """Chronological proctoring events for a handful of attempts (single-enrollment drill-down only)."""
    if not attempt_ids:
        return {}
    rows = (
        db.query(AssessmentLog)
        .filter(AssessmentLog.attempt_id.in_(attempt_ids))
        .order_by(AssessmentLog.event_time.asc(), AssessmentLog.id.asc())
        .all()
    )
    grouped: dict[int, list] = {}
    for row in rows:
        grouped.setdefault(int(row.attempt_id), []).append(row)
    return grouped


def _to_candidate_attempt_result(attempt: AssessmentAttempt) -> CandidateAttemptResultResponse:
    resp = CandidateAttemptResultResponse.model_validate(attempt)
    if resp.result_summary:
        resp.result_summary = sanitize_result_summary_for_candidate(resp.result_summary)
    return resp


def _to_evaluator_attempt_result(
    attempt: AssessmentAttempt,
    event_counts: Optional[dict[str, int]] = None,
    strip_item_diagnostics: bool = False,
    recording_status: Optional[str] = None,
    logs: Optional[list] = None,
) -> AttemptResultResponse:
    summary = attempt.result_summary
    if strip_item_diagnostics and isinstance(summary, dict) and "item_diagnostics" in summary:
        summary = dict(summary)
        summary.pop("item_diagnostics", None)
    elif not strip_item_diagnostics and isinstance(summary, dict) and "item_diagnostics" in summary:
        items = summary.get("item_diagnostics") or []
        if any(not (i.get("options") if isinstance(i, dict) else None) for i in items):
            snap = getattr(attempt, "question_snapshot", None) or {}
            qs = snap.get("questions") or []
            if qs:
                q_map = {str(q.get("question_id")): q for q in qs if isinstance(q, dict) and "question_id" in q}
                enriched = []
                for item in items:
                    if isinstance(item, dict):
                        q_data = q_map.get(str(item.get("question_id"))) or {}
                        enriched_item = dict(item)
                        if not enriched_item.get("question") and q_data.get("question"):
                            enriched_item["question"] = q_data.get("question")
                        if not enriched_item.get("options") and q_data.get("options"):
                            enriched_item["options"] = list(q_data.get("options") or [])
                        if not enriched_item.get("explanation") and q_data.get("explanation"):
                            enriched_item["explanation"] = q_data.get("explanation")
                        enriched.append(enriched_item)
                    else:
                        enriched.append(item)
                summary = dict(summary)
                summary["item_diagnostics"] = enriched

    if event_counts is not None:
        return AttemptResultResponse(
            id=attempt.id,
            attempt_number=attempt.attempt_number,
            status=attempt.status,
            score=attempt.score,
            passed=attempt.passed,
            time_spent_seconds=attempt.time_spent_seconds,
            camera_verified=bool(attempt.camera_verified),
            is_flagged=bool(attempt.is_flagged),
            flag_reason=attempt.flag_reason,
            tab_switch_count=event_counts.get("tab_switch", 0),
            window_blur_count=event_counts.get("window_blur", 0),
            camera_lost_count=event_counts.get("camera_lost", 0),
            result_summary=summary,
            started_at=attempt.started_at,
            submitted_at=attempt.submitted_at,
            recording_status=recording_status,
            logs=[AssessmentLogResponse.model_validate(l) for l in (logs or [])],
        )

    resp = AttemptResultResponse.model_validate(attempt)
    if strip_item_diagnostics and isinstance(resp.result_summary, dict):
        resp.result_summary = dict(resp.result_summary)
        resp.result_summary.pop("item_diagnostics", None)
    return resp


# Per-assessment "questions per sitting" for candidate cards. Counting needs the (large) question bank, so
# cache by (version, updated_at): any edit changes the key and self-invalidates. Bounded + lock-protected.
_QCOUNT_CACHE: dict[int, tuple[tuple, Optional[int], float]] = {}
_QCOUNT_LOCK = threading.Lock()
_QCOUNT_TTL_SECONDS = 300.0
_QCOUNT_MAX_ENTRIES = 500


def _question_counts(db: Session, assessments) -> dict[int, Optional[int]]:
    """Questions a candidate will actually get per sitting (sum of group draw counts); None for typing."""
    now = time.monotonic()
    result: dict[int, Optional[int]] = {}
    missing: list[tuple[int, str, tuple]] = []
    with _QCOUNT_LOCK:
        for a in assessments:
            if a is None or a.id in result:
                continue
            key = (a.version, str(a.updated_at))
            hit = _QCOUNT_CACHE.get(a.id)
            if hit and hit[0] == key and hit[2] > now:
                result[a.id] = hit[1]
            else:
                missing.append((a.id, a.test_type, key))

    if missing:
        mcq_ids = [m[0] for m in missing if m[1] == "mcq"]
        cfg_by_id: dict[int, dict] = {}
        if mcq_ids:
            rows = db.query(Assessment.id, Assessment.test_config).filter(Assessment.id.in_(mcq_ids)).all()
            cfg_by_id = {r[0]: (r[1] or {}) for r in rows}
        with _QCOUNT_LOCK:
            if len(_QCOUNT_CACHE) >= _QCOUNT_MAX_ENTRIES:
                for k in [k for k, v in _QCOUNT_CACHE.items() if v[2] <= now] or list(_QCOUNT_CACHE):
                    _QCOUNT_CACHE.pop(k, None)
            for aid, ttype, key in missing:
                count = _blueprint_counts(ttype, cfg_by_id.get(aid, {}))["questions_count"] if ttype == "mcq" else None
                result[aid] = count
                _QCOUNT_CACHE[aid] = (key, count, now + _QCOUNT_TTL_SECONDS)
    return result


def _build_candidate_scorecard(
    enr: AssessmentEnrollment,
    employee_name: Optional[str],
    question_count: Optional[int] = None,
) -> CandidateScorecardResponse:
    asm = enr.assessment
    base_max = (
        max(int(asm.max_attempts or 1), int(enr.max_attempts_snapshot or 0))
        if asm
        else int(enr.max_attempts_snapshot or 1)
    )
    return CandidateScorecardResponse(
        enrollment_id=enr.id,
        assignment_id=enr.assignment_id,
        assessment_id=enr.assessment_id,
        assessment_title=asm.title if asm else "Assessment",
        test_type=asm.test_type if asm else "mcq",
        duration_minutes=asm.duration_minutes if asm else 20,
        passing_score=asm.passing_score if asm else None,
        question_count=question_count,
        employee_id=enr.employee_id,
        employee_name=employee_name,
        status=enr.status,
        deadline=_effective_deadline(enr),
        attempts_used=enr.attempts_used,
        max_attempts=base_max,
        extra_attempts=enr.extra_attempts,
        best_score=enr.best_score,
        attempts=[_to_candidate_attempt_result(att) for att in (enr.attempts or [])],
    )


def _build_evaluator_scorecard(
    enr: AssessmentEnrollment,
    employee_name: Optional[str],
    event_counts_by_attempt: Optional[dict[int, dict[str, int]]] = None,
    strip_item_diagnostics: bool = False,
    latest_attempt_only: bool = False,
    recording_status_by_attempt: Optional[dict[int, str]] = None,
    logs_by_attempt: Optional[dict[int, list]] = None,
) -> ScorecardResponse:
    asm = enr.assessment
    all_attempts = enr.attempts or []
    base_max = (
        max(int(asm.max_attempts or 1), int(enr.max_attempts_snapshot or 0))
        if asm
        else int(enr.max_attempts_snapshot or 1)
    )
    if event_counts_by_attempt is not None:
        tab_switches = sum(
            event_counts_by_attempt.get(att.id, {}).get("tab_switch", 0)
            for att in all_attempts
        )
    else:
        tab_switches = sum(att.tab_switch_count for att in all_attempts)
    any_flagged = any(bool(att.is_flagged) for att in all_attempts)

    shown_attempts = all_attempts[-1:] if (latest_attempt_only and all_attempts) else all_attempts
    return ScorecardResponse(
        enrollment_id=enr.id,
        assignment_id=enr.assignment_id,
        assessment_id=enr.assessment_id,
        assessment_title=asm.title if asm else "Assessment",
        test_type=asm.test_type if asm else "mcq",
        duration_minutes=asm.duration_minutes if asm else 20,
        passing_score=asm.passing_score if asm else None,
        employee_id=enr.employee_id,
        employee_name=employee_name,
        status=enr.status,
        deadline=_effective_deadline(enr),
        attempts_used=enr.attempts_used,
        max_attempts=base_max,
        extra_attempts=enr.extra_attempts,
        best_score=enr.best_score,
        tab_switch_count=tab_switches,
        is_flagged=any_flagged,
        cluster_name=(
            f"{enr.assignment.cluster.name} (Archived)"
            if (enr.assignment and enr.assignment.cluster and enr.assignment.cluster.is_archived)
            else enr.assignment.cluster.name
            if (enr.assignment and enr.assignment.cluster)
            else None
        ),
        assignment_created_at=utc(enr.assignment.created_at) if enr.assignment else None,
        employee_designation=enr.employee.designation if enr.employee else None,
        attempts=[
            _to_evaluator_attempt_result(
                att,
                event_counts=(
                    event_counts_by_attempt.get(att.id, {})
                    if event_counts_by_attempt is not None
                    else None
                ),
                strip_item_diagnostics=strip_item_diagnostics,
                recording_status=(recording_status_by_attempt or {}).get(att.id),
                logs=(logs_by_attempt or {}).get(att.id),
            )
            for att in shown_attempts
        ],
    )


def _serialize_admin_enrollment_row(
    enr: AssessmentEnrollment,
    event_counts_by_attempt: Optional[dict[int, dict[str, int]]] = None,
    latest_attempt_only: bool = False,
    strip_item_diagnostics: bool = True,
    recording_status_by_attempt: Optional[dict[int, str]] = None,
) -> dict:
    """Flattened evaluator scorecard row with UI register & Cache Surgery metadata."""
    data = _build_evaluator_scorecard(
        enr,
        enr.employee.name if enr.employee else None,
        event_counts_by_attempt=event_counts_by_attempt,
        strip_item_diagnostics=strip_item_diagnostics,
        latest_attempt_only=latest_attempt_only,
        recording_status_by_attempt=recording_status_by_attempt,
    ).model_dump()
    attempts = enr.attempts or []
    flag_reason = next(
        (att.flag_reason for att in reversed(attempts) if att.is_flagged and att.flag_reason),
        None,
    )
    asg_created = utc(enr.assignment.created_at) if enr.assignment else None
    latest_att = attempts[-1] if attempts else None
    latest_att_start = utc(latest_att.started_at or latest_att.submitted_at) if latest_att else None
    latest_att_ts = utc(latest_att.submitted_at or latest_att.started_at) if latest_att else None
    is_prior_attempt = bool(
        latest_att_start and asg_created and latest_att_start < asg_created
    )
    data.update({
        "employee_email": enr.employee.email if enr.employee else None,
        # Employee is already joined-loaded for this row, so the photo URL costs no extra query.
        "employee_avatar_url": enr.employee.avatar_url if enr.employee else None,
        "cluster_id": enr.assignment.cluster_id if enr.assignment else None,
        "cluster_name": (
            f"{enr.assignment.cluster.name} (Archived)"
            if (enr.assignment and enr.assignment.cluster and enr.assignment.cluster.is_archived)
            else enr.assignment.cluster.name
            if (enr.assignment and enr.assignment.cluster)
            else None
        ),
        "assignment_created_at": asg_created,
        "latest_attempt_at": latest_att_ts,
        "is_prior_attempt": is_prior_attempt,
        "flag_reason": flag_reason,
        "effective_deadline": data.get("deadline"),
        "latest_attempt_id": attempts[-1].id if attempts else None,
        "latest_attempt_number": attempts[-1].attempt_number if attempts else None,
        "latest_attempt_status": attempts[-1].status if attempts else None,
        "latest_attempt_passed": attempts[-1].passed if attempts else None,
        "latest_time_spent_seconds": attempts[-1].time_spent_seconds if attempts else 0,
        "recording_status": (
            (recording_status_by_attempt or {}).get(attempts[-1].id) if attempts else None
        ),
        "updated_at": utc(enr.updated_at),
    })
    return data


def _dispatch_assessment_reminder_dms(targets: list) -> None:
    """
    Lightweight BackgroundTasks fallback used only when ARQ Redis is unavailable.
    Runs strictly AFTER db.commit() & connection release without blocking thread sleeps.
    Paces requests with 0.5s delays and handles SlackRateLimitError backoffs safely.
    """
    for idx, item in enumerate(targets):
        if isinstance(item, dict):
            slack_uid = item.get("slack_user_id")
            emp_email = item.get("employee_email")
            emp_name = item.get("employee_name") or ""
            title = item.get("assessment_title") or "Assessment"
            deadline_str = item.get("deadline_str") or "As soon as possible"
            enr_id = item.get("enrollment_id")
            overdue = bool(item.get("overdue", False))
        else:
            slack_uid, emp_email, emp_name, title, deadline_str, enr_id, overdue = item
        try:
            try_send_assessment_reminder_dm(
                slack_uid,
                emp_name,
                title,
                deadline_str,
                enr_id,
                overdue=overdue,
                employee_email=emp_email,
            )
        except SlackRateLimitError as exc:
            logger.warning(f"[REMINDERS] Slack rate limit hit for enrollment {enr_id}. Sleeping {exc.retry_after_seconds}s")
            time.sleep(exc.retry_after_seconds)
            try:
                try_send_assessment_reminder_dm(
                    slack_uid,
                    emp_name,
                    title,
                    deadline_str,
                    enr_id,
                    overdue=overdue,
                    employee_email=emp_email,
                )
            except Exception as retry_exc:
                logger.warning(f"[REMINDERS] Retry Slack DM dispatch failed for enrollment {enr_id}: {retry_exc}")
        except Exception as exc:
            logger.warning(f"[REMINDERS] Slack DM dispatch failed for enrollment {enr_id}: {exc}")
        if idx < len(targets) - 1:
            time.sleep(0.5)


def _get_scoped_evaluator_emp_ids(db: Session, current_user: User) -> Optional[set[int]]:
    """
    Mirrors leaves.py hierarchy:
    - admin / hr -> None (full access across company)
    - pm / team_lead -> strictly manageable subordinates on their projects (exclude_self=True)
    """
    if current_user.role in ("admin", "hr"):
        return None
    if current_user.role in ("pm", "team_lead"):
        return project_scope.get_manageable_employee_ids(db, current_user, exclude_self=True)
    return set()


def _ensure_evaluator_can_view_employee(
    db: Session,
    current_user: User,
    target_employee_id: int,
) -> None:
    if current_user.role in ("admin", "hr"):
        return
    if current_user.role in ("pm", "team_lead"):
        if current_user.employee_id and current_user.employee_id == target_employee_id:
            raise HTTPException(
                status_code=403,
                detail="Access denied. Cannot view evaluator telemetry or recordings for your own assessment.",
            )
        scoped_ids = _get_scoped_evaluator_emp_ids(db, current_user)
        if scoped_ids is not None and target_employee_id not in scoped_ids:
            raise HTTPException(
                status_code=403,
                detail="Access denied. You may only view assessment records for members in your managed hierarchy.",
            )
        return
    raise HTTPException(status_code=403, detail="Access denied.")


def _ingest_telemetry_events(
    db: Session,
    attempt_id: int,
    raw_events: Optional[list],
    now: datetime,
) -> int:
    if not raw_events:
        return 0
    existing_count = (
        db.query(func.count(AssessmentLog.id))
        .filter_by(attempt_id=attempt_id)
        .scalar()
        or 0
    )
    remaining_budget = max(0, MAX_TELEMETRY_LOGS_PER_ATTEMPT - int(existing_count))
    if remaining_budget <= 0:
        return 0

    count = 0
    for ev in raw_events[:50]:
        if count >= remaining_budget:
            break
        if hasattr(ev, "model_dump"):
            ev = ev.model_dump()
        if not isinstance(ev, dict):
            continue
        etype = ev.get("event_type")
        if etype not in VALID_TELEMETRY_EVENTS:
            continue
        raw_details = ev.get("details")
        details = raw_details if isinstance(raw_details, dict) else {}
        db.add(AssessmentLog(
            attempt_id=attempt_id,
            event_type=etype,
            event_time=now,
            details=details,
        ))
        count += 1
    return count


# ============================================================================
# 1. Blueprint Studio (Admin & HR Only)
# ============================================================================

def _ensure_unique_active_assessment_title(
    db: Session,
    title: str,
    exclude_id: Optional[int] = None,
) -> str:
    """Enforces case-insensitive uniqueness across non-archived assessments."""
    clean_title = (title or "").strip()
    if not clean_title:
        raise HTTPException(status_code=422, detail="Assessment title cannot be empty.")
    query = db.query(Assessment.id).filter(
        func.lower(Assessment.title) == clean_title.lower(),
        Assessment.status != "archived",
    )
    if exclude_id is not None:
        query = query.filter(Assessment.id != exclude_id)
    if query.first():
        raise HTTPException(
            status_code=409,
            detail=(
                f"An active assessment named '{clean_title}' already exists. "
                "Please enter a different title."
            ),
        )
    return clean_title


@router.post("", response_model=AssessmentDetailResponse, status_code=status.HTTP_201_CREATED)
def create_assessment(
    payload: AssessmentCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """Admin/HR only: Creates a new assessment blueprint in draft state."""
    clean_title = _ensure_unique_active_assessment_title(db, payload.title)
    assessment = Assessment(
        title=clean_title,
        description=payload.description,
        test_type=payload.test_type,
        status="draft",
        version=1,
        duration_minutes=payload.duration_minutes,
        passing_score=payload.passing_score,
        max_attempts=payload.max_attempts,
        test_config=payload.test_config,
        proctoring_config=payload.proctoring_config,
        created_by=current_user.id,
    )
    db.add(assessment)
    db.flush()

    audit_service.record(
        db,
        actor=current_user,
        action="assessment.created",
        category="Assessments",
        action_type="Created",
        entity_type="assessment",
        entity_id=assessment.id,
        entity_name=assessment.title,
        summary=f"Created draft {assessment.test_type} assessment '{assessment.title}'",
        request=request,
    )

    db.commit()
    db.refresh(assessment)
    return assessment


def _blueprint_counts(test_type: str, cfg: dict) -> dict:
    """Group / draw / pool totals so list cards can render without re-parsing the question bank."""
    if test_type != "mcq" or not isinstance(cfg, dict):
        return {"categories_count": 0, "questions_count": 0, "pool_size": 0}
    cats = [c for c in (cfg.get("categories") or []) if isinstance(c, dict)]
    if cats:
        pool = 0
        draw = 0
        for c in cats:
            pool += len(c.get("questions") or [])
            try:
                pick = int(c.get("pick_count") or 0)
            except (ValueError, TypeError):
                pick = 0
            draw += max(0, pick)
        return {"categories_count": len(cats), "questions_count": draw, "pool_size": pool}

    # Flat questions fallback for legacy/imported blueprints
    flat_questions = cfg.get("questions") or []
    if isinstance(flat_questions, list) and flat_questions:
        return {"categories_count": 0, "questions_count": len(flat_questions), "pool_size": len(flat_questions)}

    return {"categories_count": 0, "questions_count": 0, "pool_size": 0}


@router.get("", response_model=List[AssessmentListResponse])
def list_assessments(
    status_filter: Optional[str] = Query(None, alias="status"),
    test_type: Optional[str] = Query(None),
    include_archived: bool = Query(False),
    include_config: bool = Query(
        False,
        description="Include the full question bank (test_config) and group/pool counts. "
        "Off by default so plain listings stay lightweight.",
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """Admin/HR only: Lists assessment blueprints; the heavy test_config is opt-in via include_config."""
    query = db.query(Assessment)
    if not include_config:
        query = query.options(defer(Assessment.test_config))
    if status_filter:
        query = query.filter(Assessment.status == status_filter)
    elif not include_archived:
        query = query.filter(Assessment.status != "archived")
    if test_type:
        query = query.filter(Assessment.test_type == test_type)

    rows = (
        query.order_by(Assessment.created_at.desc(), Assessment.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    items = []
    for a in rows:
        item = {
            "id": a.id,
            "title": a.title,
            "description": a.description,
            "test_type": a.test_type,
            "status": a.status,
            "version": a.version,
            "duration_minutes": a.duration_minutes,
            "passing_score": a.passing_score,
            "max_attempts": a.max_attempts,
            "proctoring_config": a.proctoring_config,
            "created_at": a.created_at,
        }
        if include_config:
            cfg = a.test_config or {}
            item["test_config"] = cfg
            item.update(_blueprint_counts(a.test_type, cfg))
        items.append(item)
    return items


# ============================================================================
# 2. Candidate Dashboards & Evaluator Scoped Scorecards (Static Routes First)
# ============================================================================

@router.get("/my/enrollments", response_model=List[CandidateScorecardResponse])
def get_my_assessments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Candidate endpoint: Lists all assessments assigned to the current employee (answer keys & flags stripped)."""
    if not current_user.employee_id:
        return []

    enrollments = (
        db.query(AssessmentEnrollment)
        .options(
            joinedload(AssessmentEnrollment.assessment).defer(Assessment.test_config),
            joinedload(AssessmentEnrollment.assignment),
            selectinload(AssessmentEnrollment.attempts)
            .defer(AssessmentAttempt.question_snapshot)
            .defer(AssessmentAttempt.answers),
        )
        .filter(
            AssessmentEnrollment.employee_id == current_user.employee_id,
            AssessmentEnrollment.status != "cancelled",
        )
        .all()
    )

    counts = _question_counts(db, [enr.assessment for enr in enrollments])
    return [
        _build_candidate_scorecard(enr, current_user.name, counts.get(enr.assessment_id))
        for enr in enrollments
    ]


@router.get("/scorecards", response_model=List[ScorecardResponse])
def list_team_scorecards(
    assessment_id: Optional[int] = Query(None),
    assignment_id: Optional[int] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    flagged_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_viewer),
):
    """
    Evaluator endpoint: Lists scorecards & proctoring summaries.
    - Admin & HR: Company-wide view.
    - PM & Team Lead: Strictly scoped to their manageable team hierarchy (matching leaves.py).
    """
    scoped_emp_ids = _get_scoped_evaluator_emp_ids(db, current_user)
    if scoped_emp_ids is not None and not scoped_emp_ids:
        return []

    query = (
        db.query(AssessmentEnrollment)
        .join(Employee, Employee.id == AssessmentEnrollment.employee_id)
        .options(
            joinedload(AssessmentEnrollment.assessment).defer(Assessment.test_config),
            joinedload(AssessmentEnrollment.assignment),
            joinedload(AssessmentEnrollment.employee).load_only(
                Employee.id, Employee.name, Employee.email, Employee.designation, Employee.avatar_url
            ),
            selectinload(AssessmentEnrollment.attempts)
            .defer(AssessmentAttempt.question_snapshot)
            .defer(AssessmentAttempt.answers),
        )
        .filter(
            AssessmentEnrollment.status != "cancelled",
            Employee.status != "archived",
        )
    )

    if scoped_emp_ids is not None:
        query = query.filter(AssessmentEnrollment.employee_id.in_(scoped_emp_ids))
    if assessment_id is not None:
        query = query.filter(AssessmentEnrollment.assessment_id == assessment_id)
    if assignment_id is not None:
        query = query.filter(AssessmentEnrollment.assignment_id == assignment_id)
    if status_filter:
        query = query.filter(AssessmentEnrollment.status == status_filter)
    if flagged_only:
        flagged_enr_ids = (
            db.query(AssessmentAttempt.enrollment_id)
            .filter(AssessmentAttempt.is_flagged == True)
            .scalar_subquery()
        )
        query = query.filter(AssessmentEnrollment.id.in_(flagged_enr_ids))

    enrollments = (
        query.order_by(AssessmentEnrollment.updated_at.desc(), AssessmentEnrollment.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    attempt_ids = [att.id for enr in enrollments for att in (enr.attempts or [])]
    event_counts = _fetch_attempt_event_counts(db, attempt_ids)
    return [
        _build_evaluator_scorecard(
            enr,
            enr.employee.name if enr.employee else None,
            event_counts_by_attempt=event_counts,
            strip_item_diagnostics=True,
        )
        for enr in enrollments
    ]


@router.get("/admin/enrollments")
def list_admin_enrollments(
    cluster_id: Optional[int] = Query(None),
    assessment_id: Optional[int] = Query(None),
    assignment_id: Optional[int] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    flagged_only: bool = Query(False),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_viewer),
):
    """
    Scoped Progress & Logs register (paginated + summary KPIs).
    - Admin/HR: company-wide enrollments.
    - PM/Team Lead: strictly subordinate team members in project_scope (never self or peers).
    """
    # The register's "Flagged" tab is a virtual status; map it to the flagged_only slice.
    if status_filter == "flagged":
        flagged_only = True
        status_filter = None

    scoped_emp_ids = _get_scoped_evaluator_emp_ids(db, current_user)
    if scoped_emp_ids is not None and not scoped_emp_ids:
        return {
            "items": [],
            "total_items": 0,
            "total_pages": 1,
            "page": page,
            "page_size": page_size,
            "summary": {
                "total": 0,
                "assigned": 0,
                "in_progress": 0,
                "passed": 0,
                "failed": 0,
                "flagged": 0,
                "pass_rate": None,
                "attempted": 0,
                "not_started": 0,
                "latest_passed": 0,
                "latest_failed": 0,
                "attempt_pass_rate": None,
                "ready_videos": 0,
            },
        }

    base_filters = [
        AssessmentEnrollment.status != "cancelled",
        Employee.status != "archived",
    ]
    if scoped_emp_ids is not None:
        base_filters.append(AssessmentEnrollment.employee_id.in_(scoped_emp_ids))
    if cluster_id is not None:
        cluster_asg_ids = (
            db.query(Assignment.id)
            .filter(Assignment.cluster_id == cluster_id)
            .scalar_subquery()
        )
        base_filters.append(AssessmentEnrollment.assignment_id.in_(cluster_asg_ids))
    if assessment_id is not None:
        base_filters.append(AssessmentEnrollment.assessment_id == assessment_id)
    if assignment_id is not None:
        base_filters.append(AssessmentEnrollment.assignment_id == assignment_id)
    if search and search.strip():
        term = f"%{search.strip()}%"
        base_filters.append(Employee.name.ilike(term) | Employee.email.ilike(term))

    # Summary KPIs across the base filtered scope (before status/flagged slice)
    status_rows = (
        db.query(AssessmentEnrollment.status, func.count(AssessmentEnrollment.id))
        .join(Employee, Employee.id == AssessmentEnrollment.employee_id)
        .filter(*base_filters)
        .group_by(AssessmentEnrollment.status)
        .all()
    )
    status_counts = {st: int(cnt) for st, cnt in status_rows}
    total_base = sum(status_counts.values())

    flagged_enr_ids_subq = (
        db.query(AssessmentAttempt.enrollment_id)
        .filter(AssessmentAttempt.is_flagged == True)
        .scalar_subquery()
    )
    flagged_count = (
        db.query(func.count(AssessmentEnrollment.id))
        .join(Employee, Employee.id == AssessmentEnrollment.employee_id)
        .filter(*base_filters, AssessmentEnrollment.id.in_(flagged_enr_ids_subq))
        .scalar()
    ) or 0 if total_base > 0 else 0

    passed_n = status_counts.get("passed", 0)
    failed_n = status_counts.get("failed", 0)
    attempted_n = 0
    not_started_n = 0
    latest_passed_n = 0
    latest_failed_n = 0
    ready_videos = 0
    if total_base > 0:
        has_finished_attempt = exists().where(
            AssessmentAttempt.enrollment_id == AssessmentEnrollment.id,
            AssessmentAttempt.status.in_(["completed", "abandoned"]),
        )
        has_passed_attempt = exists().where(
            AssessmentAttempt.enrollment_id == AssessmentEnrollment.id,
            AssessmentAttempt.passed == True,
        )
        attempted_expr = or_(
            AssessmentEnrollment.status.in_(["passed", "failed"]),
            has_finished_attempt,
        )
        passed_eval_expr = or_(
            AssessmentEnrollment.status == "passed",
            has_passed_attempt,
        )
        not_started_expr = and_(
            AssessmentEnrollment.status == "assigned",
            ~has_finished_attempt,
        )
        sit_row = (
            db.query(
                func.coalesce(func.sum(case((attempted_expr, 1), else_=0)), 0),
                func.coalesce(func.sum(case((not_started_expr, 1), else_=0)), 0),
                func.coalesce(func.sum(case((and_(attempted_expr, passed_eval_expr), 1), else_=0)), 0),
            )
            .join(Employee, Employee.id == AssessmentEnrollment.employee_id)
            .filter(*base_filters)
            .first()
        )
        if sit_row:
            attempted_n = int(sit_row[0] or 0)
            not_started_n = int(sit_row[1] or 0)
            latest_passed_n = int(sit_row[2] or 0)
            latest_failed_n = max(0, attempted_n - latest_passed_n)

        ready_videos = (
            db.query(func.count(AssessmentRecording.id))
            .join(AssessmentAttempt, AssessmentAttempt.id == AssessmentRecording.attempt_id)
            .join(AssessmentEnrollment, AssessmentEnrollment.id == AssessmentAttempt.enrollment_id)
            .join(Employee, Employee.id == AssessmentEnrollment.employee_id)
            .filter(AssessmentRecording.status == "ready", *base_filters)
            .scalar()
        ) or 0

    summary = {
        "total": total_base,
        "assigned": status_counts.get("assigned", 0),
        "in_progress": status_counts.get("in_progress", 0),
        "passed": passed_n,
        "failed": failed_n,
        "flagged": int(flagged_count),
        "pass_rate": (
            round(passed_n * 100.0 / (passed_n + failed_n), 1) if (passed_n + failed_n) > 0 else None
        ),
        "attempted": attempted_n,
        "not_started": not_started_n,
        "latest_passed": latest_passed_n,
        "latest_failed": latest_failed_n,
        "attempt_pass_rate": (
            round(latest_passed_n * 100.0 / attempted_n, 1) if attempted_n > 0 else None
        ),
        "ready_videos": int(ready_videos),
    }

    if total_base == 0:
        return {
            "items": [],
            "total_items": 0,
            "total_pages": 1,
            "page": page,
            "page_size": page_size,
            "summary": summary,
        }

    row_filters = list(base_filters)
    if status_filter:
        row_filters.append(AssessmentEnrollment.status == status_filter)
    if flagged_only:
        row_filters.append(AssessmentEnrollment.id.in_(flagged_enr_ids_subq))

    offset = (page - 1) * page_size
    enrollments = (
        db.query(AssessmentEnrollment)
        .join(Employee, Employee.id == AssessmentEnrollment.employee_id)
        .options(
            joinedload(AssessmentEnrollment.assessment).defer(Assessment.test_config),
            joinedload(AssessmentEnrollment.assignment).joinedload(Assignment.cluster),
            joinedload(AssessmentEnrollment.employee).load_only(
                Employee.id, Employee.name, Employee.email, Employee.designation, Employee.avatar_url
            ),
            selectinload(AssessmentEnrollment.attempts)
            .defer(AssessmentAttempt.question_snapshot)
            .defer(AssessmentAttempt.answers),
        )
        .filter(*row_filters)
        .order_by(AssessmentEnrollment.updated_at.desc(), AssessmentEnrollment.id.desc())
        .offset(offset)
        .limit(page_size)
        .all()
    )

    # Reuse already-fetched summary counts / Page-1 fast path whenever possible
    if status_filter and flagged_only:
        if page == 1 and len(enrollments) < page_size:
            total_items = len(enrollments)
        else:
            total_items = (
                db.query(func.count(AssessmentEnrollment.id))
                .join(Employee, Employee.id == AssessmentEnrollment.employee_id)
                .filter(*row_filters)
                .scalar()
            ) or 0
    elif status_filter:
        total_items = status_counts.get(status_filter, 0)
    elif flagged_only:
        total_items = int(flagged_count)
    else:
        total_items = total_base

    attempt_ids = [att.id for enr in enrollments for att in (enr.attempts or [])]
    event_counts = _fetch_attempt_event_counts(db, attempt_ids)
    recording_statuses = _fetch_recording_statuses(db, attempt_ids)

    return {
        "items": [
            _serialize_admin_enrollment_row(
                enr,
                event_counts_by_attempt=event_counts,
                latest_attempt_only=True,
                strip_item_diagnostics=True,
                recording_status_by_attempt=recording_statuses,
            )
            for enr in enrollments
        ],
        "total_items": int(total_items),
        "total_pages": max(1, -(-int(total_items) // page_size)),
        "page": page,
        "page_size": page_size,
        "summary": summary,
    }


@router.get("/admin/enrollments/{enrollment_id}/row")
def get_admin_enrollment_row(
    enrollment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_viewer),
):
    """Surgical Cache-Surgery row refresh endpoint for a single enrollment."""
    enr = (
        db.query(AssessmentEnrollment)
        .options(
            joinedload(AssessmentEnrollment.assessment).defer(Assessment.test_config),
            joinedload(AssessmentEnrollment.assignment).joinedload(Assignment.cluster),
            joinedload(AssessmentEnrollment.employee).load_only(
                Employee.id, Employee.name, Employee.email, Employee.designation, Employee.avatar_url
            ),
            selectinload(AssessmentEnrollment.attempts)
            .defer(AssessmentAttempt.answers),
        )
        .filter_by(id=enrollment_id)
        .first()
    )
    if not enr:
        raise HTTPException(status_code=404, detail="Enrollment not found.")

    _ensure_evaluator_can_view_employee(db, current_user, enr.employee_id)
    row_attempt_ids = [att.id for att in (enr.attempts or [])]
    event_counts = _fetch_attempt_event_counts(db, row_attempt_ids)
    return _serialize_admin_enrollment_row(
        enr,
        event_counts_by_attempt=event_counts,
        latest_attempt_only=False,
        strip_item_diagnostics=False,
        recording_status_by_attempt=_fetch_recording_statuses(db, row_attempt_ids),
    )


@router.get("/admin/filters")
def get_evaluator_filter_options(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_viewer),
):
    """
    Lightweight filter dropdown data for the evaluator register, limited to the assessments and
    cohorts that actually have enrollments inside the caller's visibility scope. Lets PM/Team Lead
    filter without access to the admin-only assessment/cluster management lists.
    """
    scoped_emp_ids = _get_scoped_evaluator_emp_ids(db, current_user)
    if scoped_emp_ids is not None and not scoped_emp_ids:
        return {"assessments": [], "clusters": []}

    scope = [
        AssessmentEnrollment.status != "cancelled",
        Employee.status != "archived",
    ]
    if scoped_emp_ids is not None:
        scope.append(AssessmentEnrollment.employee_id.in_(scoped_emp_ids))

    assessment_rows = (
        db.query(Assessment.id, Assessment.title)
        .join(AssessmentEnrollment, AssessmentEnrollment.assessment_id == Assessment.id)
        .join(Employee, Employee.id == AssessmentEnrollment.employee_id)
        .filter(*scope)
        .distinct()
        .order_by(Assessment.title.asc())
        .all()
    )
    cluster_rows = (
        db.query(Cluster.id, Cluster.name, Cluster.is_archived)
        .join(Assignment, Assignment.cluster_id == Cluster.id)
        .join(AssessmentEnrollment, AssessmentEnrollment.assignment_id == Assignment.id)
        .join(Employee, Employee.id == AssessmentEnrollment.employee_id)
        .filter(*scope)
        .distinct()
        .order_by(Cluster.name.asc())
        .all()
    )
    return {
        "assessments": [{"id": a_id, "title": title} for a_id, title in assessment_rows],
        "clusters": [
            {"id": c_id, "name": f"{name} (Archived)" if is_arch else name}
            for c_id, name, is_arch in cluster_rows
        ],
    }


@router.post("/reminders/send")
async def send_manual_assessment_reminders(
    payload: ManualReminderRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """
    Admin/HR only: Sends manual in-app notifications and queues Slack DMs
    to active employees with pending/in-progress assessments.
    Commits DB state and releases session before dispatching Slack calls via ARQ.
    """
    def _prepare_reminders() -> tuple[dict, list[dict]]:
        try:
            query = (
                db.query(AssessmentEnrollment)
                .join(Employee, Employee.id == AssessmentEnrollment.employee_id)
                .join(Assessment, Assessment.id == AssessmentEnrollment.assessment_id)
                .outerjoin(Assignment, Assignment.id == AssessmentEnrollment.assignment_id)
                .options(
                    joinedload(AssessmentEnrollment.assessment).defer(Assessment.test_config),
                    joinedload(AssessmentEnrollment.assignment),
                    joinedload(AssessmentEnrollment.employee).load_only(
                        Employee.id, Employee.name, Employee.email, Employee.slack_user_id
                    ),
                )
                .filter(
                    AssessmentEnrollment.status.in_(["assigned", "in_progress"]),
                    Employee.status.in_(["active", "on-leave"]),
                    Assessment.status == "published",
                    or_(AssessmentEnrollment.assignment_id.is_(None), Assignment.is_active == True),
                )
            )
            if payload.enrollment_id is not None:
                query = query.filter(AssessmentEnrollment.id == payload.enrollment_id)
            if payload.enrollment_ids:
                query = query.filter(AssessmentEnrollment.id.in_(payload.enrollment_ids))
            if payload.assessment_id is not None:
                query = query.filter(AssessmentEnrollment.assessment_id == payload.assessment_id)
            if payload.cluster_id is not None:
                cluster_asg_ids = (
                    db.query(Assignment.id)
                    .filter(Assignment.cluster_id == payload.cluster_id, Assignment.is_active == True)
                    .scalar_subquery()
                )
                query = query.filter(AssessmentEnrollment.assignment_id.in_(cluster_asg_ids))

            enrollments = query.all()
            if not enrollments:
                return (
                    {
                        "detail": "No pending active enrollments matched the reminder filter.",
                        "notified_count": 0,
                        "slack_queued_count": 0,
                    },
                    [],
                )

            emp_ids = {enr.employee_id for enr in enrollments}
            users = (
                db.query(User)
                .filter(User.employee_id.in_(emp_ids), User.is_active == True)
                .all()
            )
            user_by_emp_id = {u.employee_id: u for u in users if u.employee_id is not None}

            now = datetime.now(timezone.utc)
            cooldown_cutoff = now - timedelta(minutes=REMINDER_COOLDOWN_MINUTES)

            recent_notif_pairs: set[tuple[int, str]] = set()
            if user_by_emp_id:
                user_ids = [u.id for u in user_by_emp_id.values()]
                recent_rows = (
                    db.query(Notification.user_id, Notification.title)
                    .filter(
                        Notification.user_id.in_(user_ids),
                        Notification.type == "assessment_reminder",
                        Notification.created_at >= cooldown_cutoff,
                    )
                    .all()
                )
                recent_notif_pairs = {(int(uid), str(t)) for uid, t in recent_rows}

            in_app_count = 0
            notified_count = 0
            skipped_cooldown_count = 0
            slack_targets_local: list[dict] = []

            for enr in enrollments:
                emp = enr.employee
                asm = enr.assessment
                if not emp or not asm:
                    continue

                eff_dl = _effective_deadline(enr)
                is_overdue = bool(eff_dl and eff_dl <= now)
                if eff_dl:
                    dl_ist = eff_dl.astimezone(IST)
                    dl_str = dl_ist.strftime("%d %b %Y, %I:%M %p IST")
                else:
                    dl_str = "As soon as possible"

                notif_title = f"{'Overdue' if is_overdue else 'Reminder'}: {asm.title}"
                user = user_by_emp_id.get(enr.employee_id)

                last_reminded = utc(enr.last_reminded_at) if enr.last_reminded_at else None
                in_cooldown = (last_reminded is not None and last_reminded > cooldown_cutoff) or (
                    user is not None and (user.id, notif_title) in recent_notif_pairs
                )
                if in_cooldown:
                    skipped_cooldown_count += 1
                    continue

                if user:
                    db.add(
                        Notification(
                            user_id=user.id,
                            title=notif_title,
                            message=(
                                f"Your assessment '{asm.title}' "
                                f"{'was due' if is_overdue else 'is due'} by {dl_str}. "
                                f"Please complete it from My Assessments."
                            ),
                            type="assessment_reminder",
                            is_read=False,
                        )
                    )
                    in_app_count += 1
                    recent_notif_pairs.add((user.id, notif_title))

                has_slack_target = bool(emp.slack_user_id or emp.email)
                if has_slack_target:
                    slack_targets_local.append({
                        "slack_user_id": emp.slack_user_id,
                        "employee_email": emp.email,
                        "employee_name": emp.name,
                        "assessment_title": asm.title,
                        "deadline_str": dl_str,
                        "enrollment_id": enr.id,
                        "overdue": is_overdue,
                    })

                if user or has_slack_target:
                    notified_count += 1
                    enr.last_reminded_at = now
                    enr.updated_at = now

            if notified_count > 0:
                audit_service.record(
                    db,
                    actor=current_user,
                    action="assessment.reminders_sent",
                    category="Assessments",
                    action_type="Created",
                    entity_type="assessment" if payload.assessment_id else ("cluster" if payload.cluster_id else "enrollment"),
                    entity_id=payload.assessment_id or payload.cluster_id or payload.enrollment_id,
                    summary=(
                        f"Sent manual assessment reminders to {notified_count} candidate(s) "
                        f"({in_app_count} in-app, {len(slack_targets_local)} Slack targets, "
                        f"{skipped_cooldown_count} skipped by cooldown)"
                    ),
                    details=[
                        {
                            "field": "reminder_scope",
                            "new": (
                                f"cluster_id={payload.cluster_id}, assessment_id={payload.assessment_id}, "
                                f"enrollment_id={payload.enrollment_id}, notified={notified_count}, "
                                f"in_app={in_app_count}, slack_queued={len(slack_targets_local)}, "
                                f"skipped_cooldown={skipped_cooldown_count}"
                            ),
                        }
                    ],
                    request=request,
                )

            db.commit()
            return (
                {
                    "detail": f"Queued reminders for {notified_count} enrollment(s).",
                    "notified_count": notified_count,
                    "in_app_count": in_app_count,
                    "slack_queued_count": len(slack_targets_local),
                    "skipped_cooldown_count": skipped_cooldown_count,
                },
                slack_targets_local,
            )
        finally:
            db.close()

    result_payload, slack_targets = await anyio.to_thread.run_sync(_prepare_reminders)

    if slack_targets:
        redis_pool = getattr(request.app.state, "redis_pool", None)
        if redis_pool is not None:
            try:
                await redis_pool.enqueue_job("send_assessment_reminders_task", slack_targets)
            except Exception as exc:
                logger.warning("[REMINDERS] Redis enqueue failed, falling back to BackgroundTasks: %s", exc)
                background_tasks.add_task(_dispatch_assessment_reminder_dms, slack_targets)
        else:
            background_tasks.add_task(_dispatch_assessment_reminder_dms, slack_targets)

    return result_payload


@router.get("/{assessment_id}/scorecards", response_model=List[ScorecardResponse])
def list_assessment_scorecards(
    assessment_id: int,
    status_filter: Optional[str] = Query(None, alias="status"),
    flagged_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_viewer),
):
    """Scoped evaluator endpoint returning scorecards for a specific assessment."""
    return list_team_scorecards(
        assessment_id=assessment_id,
        assignment_id=None,
        status_filter=status_filter,
        flagged_only=flagged_only,
        limit=limit,
        offset=offset,
        db=db,
        current_user=current_user,
    )


@router.get("/{assessment_id}", response_model=AssessmentDetailResponse)
def get_assessment_blueprint(
    assessment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """Admin/HR only: Returns complete blueprint including answers, category pools, and explanations."""
    assessment = db.query(Assessment).filter_by(id=assessment_id).first()
    if not assessment:
        raise HTTPException(status_code=404, detail="Assessment not found.")
    return assessment


@router.get("/{assessment_id}/overview", response_model=AssessmentOverviewResponse)
def get_assessment_overview(
    assessment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Assessment overview without questions or answer keys.
    - Admin/HR: can inspect any assessment overview.
    - Candidates & PM/Team Lead: only if assessment is published and caller has a non-cancelled
      enrollment (or manages enrolled members for PM/Team Lead).
    """
    assessment = db.query(Assessment).options(defer(Assessment.test_config)).filter_by(id=assessment_id).first()
    if not assessment:
        raise HTTPException(status_code=404, detail="Assessment not found.")

    if current_user.role in ("admin", "hr"):
        return assessment

    if assessment.status != "published":
        raise HTTPException(status_code=403, detail="Access denied. Assessment is not published.")

    has_own_enrollment = bool(
        current_user.employee_id
        and db.query(AssessmentEnrollment.id)
        .filter(
            AssessmentEnrollment.assessment_id == assessment_id,
            AssessmentEnrollment.employee_id == current_user.employee_id,
            AssessmentEnrollment.status != "cancelled",
        )
        .first()
    )
    if has_own_enrollment:
        return assessment

    if current_user.role in ("pm", "team_lead"):
        scoped_emp_ids = _get_scoped_evaluator_emp_ids(db, current_user)
        if scoped_emp_ids:
            has_team_enr = bool(
                db.query(AssessmentEnrollment.id)
                .filter(
                    AssessmentEnrollment.assessment_id == assessment_id,
                    AssessmentEnrollment.employee_id.in_(scoped_emp_ids),
                    AssessmentEnrollment.status != "cancelled",
                )
                .first()
            )
            if has_team_enr:
                return assessment

    raise HTTPException(status_code=403, detail="Access denied. You are not enrolled in this assessment.")


@router.put("/{assessment_id}", response_model=AssessmentDetailResponse)
def update_assessment(
    assessment_id: int,
    payload: AssessmentUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """Admin/HR only: Updates assessment blueprint. Enforces publish validation when publishing."""
    try:
        _set_lock_timeout(db)
        assessment = (
            db.query(Assessment)
            .filter_by(id=assessment_id)
            .with_for_update(of=Assessment)
            .first()
        )
    except OperationalError as exc:
        if _is_lock_contention_error(exc):
            db.rollback()
            raise _lock_conflict_http_exception()
        raise
    if not assessment:
        raise HTTPException(status_code=404, detail="Assessment not found.")

    now = datetime.now(timezone.utc)
    old_status = assessment.status
    old_max_attempts = assessment.max_attempts

    target_config = payload.test_config if payload.test_config is not None else assessment.test_config
    target_status = payload.status if payload.status is not None else assessment.status
    target_passing = payload.passing_score if payload.passing_score is not None else assessment.passing_score

    if target_status == "published":
        try:
            validate_blueprint_for_publish(
                assessment.test_type,
                target_config,
                passing_score=target_passing,
            )
        except AssessmentValidationError as e:
            raise HTTPException(status_code=422, detail=str(e))

    # Version bumps only when published exam content/thresholds/timing/proctoring actually change and enrollments exist
    enrollments_exist = bool(
        db.query(AssessmentEnrollment.id).filter_by(assessment_id=assessment.id).first()
    )
    exam_definition_changed = (
        (payload.test_config is not None and payload.test_config != assessment.test_config)
        or (payload.passing_score is not None and payload.passing_score != assessment.passing_score)
        or (payload.duration_minutes is not None and payload.duration_minutes != assessment.duration_minutes)
        or (payload.proctoring_config is not None and payload.proctoring_config != assessment.proctoring_config)
        or (old_status != "published" and target_status == "published")
    )

    if enrollments_exist and exam_definition_changed and (old_status == "published" or target_status == "published"):
        assessment.version += 1

    # Preserve attempt allowances on existing non-cancelled enrollments if max_attempts is reduced
    if payload.max_attempts is not None and payload.max_attempts < old_max_attempts:
        db.query(AssessmentEnrollment).filter(
            AssessmentEnrollment.assessment_id == assessment.id,
            AssessmentEnrollment.status != "cancelled",
            or_(
                AssessmentEnrollment.max_attempts_snapshot.is_(None),
                AssessmentEnrollment.max_attempts_snapshot < old_max_attempts,
            ),
        ).update(
            {"max_attempts_snapshot": old_max_attempts, "updated_at": now},
            synchronize_session=False,
        )

    if payload.title is not None:
        if target_status != "archived":
            assessment.title = _ensure_unique_active_assessment_title(
                db, payload.title, exclude_id=assessment.id
            )
        else:
            assessment.title = payload.title
    elif target_status != "archived" and old_status == "archived":
        _ensure_unique_active_assessment_title(db, assessment.title, exclude_id=assessment.id)
    if payload.description is not None:
        assessment.description = payload.description
    if payload.status is not None:
        assessment.status = payload.status
    if payload.duration_minutes is not None:
        assessment.duration_minutes = payload.duration_minutes
    if payload.passing_score is not None:
        assessment.passing_score = payload.passing_score
    if payload.max_attempts is not None:
        assessment.max_attempts = payload.max_attempts
    if payload.test_config is not None:
        assessment.test_config = payload.test_config
    if payload.proctoring_config is not None:
        assessment.proctoring_config = payload.proctoring_config

    if (target_status == "archived" and old_status != "archived") or (
        old_status == "published" and target_status == "draft"
    ):
        archive_assessment_cascade(db, assessment.id, now=now)

    action_key = (
        "assessment.published" if (old_status != "published" and target_status == "published")
        else "assessment.archived" if (old_status != "archived" and target_status == "archived")
        else "assessment.updated"
    )
    audit_service.record(
        db,
        actor=current_user,
        action=action_key,
        category="Assessments",
        action_type="Archived" if target_status == "archived" else "Updated",
        entity_type="assessment",
        entity_id=assessment.id,
        entity_name=assessment.title,
        summary=f"Updated assessment '{assessment.title}' (status={assessment.status}, v{assessment.version})",
        request=request,
    )

    assessment.updated_at = now
    with _QCOUNT_LOCK:
        _QCOUNT_CACHE.pop(assessment.id, None)

    db.commit()
    db.refresh(assessment)
    return assessment


@router.delete("/{assessment_id}", status_code=status.HTTP_200_OK)
def archive_assessment(
    assessment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """Admin/HR only: Soft-archives an assessment, deactivates its assignments, and cancels unstarted enrollments."""
    assessment = db.query(Assessment).options(defer(Assessment.test_config)).filter_by(id=assessment_id).first()
    if not assessment:
        raise HTTPException(status_code=404, detail="Assessment not found.")

    now = datetime.now(timezone.utc)
    assessment.status = "archived"
    assessment.updated_at = now
    archive_assessment_cascade(db, assessment.id, now=now)

    audit_service.record(
        db,
        actor=current_user,
        action="assessment.archived",
        category="Assessments",
        action_type="Archived",
        entity_type="assessment",
        entity_id=assessment.id,
        entity_name=assessment.title,
        summary=f"Archived assessment '{assessment.title}' and deactivated active assignments",
        request=request,
    )

    db.commit()
    return {"detail": "Assessment archived successfully."}


@router.post("/{assessment_id}/restore", response_model=AssessmentDetailResponse, status_code=status.HTTP_200_OK)
def restore_assessment(
    assessment_id: int,
    request: Request,
    payload: Optional[AssessmentRestore] = Body(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """
    Admin/HR only: Restores an archived assessment to 'draft' or 'published'.
    When restoring directly to 'published' with `restore_assignments=True`, non-expired
    deactivated cluster assignments are automatically re-activated cleanly.
    """
    try:
        _set_lock_timeout(db)
        assessment = (
            db.query(Assessment)
            .filter_by(id=assessment_id)
            .with_for_update(of=Assessment)
            .first()
        )
    except OperationalError as exc:
        if _is_lock_contention_error(exc):
            db.rollback()
            raise _lock_conflict_http_exception()
        raise
    if not assessment:
        raise HTTPException(status_code=404, detail="Assessment not found.")
    if assessment.status != "archived":
        raise HTTPException(status_code=409, detail="Only archived assessments can be restored.")

    target_status = payload.restore_to if payload else "draft"
    want_assignments = bool(payload and payload.restore_assignments)
    target_title = _ensure_unique_active_assessment_title(
        db,
        payload.title if (payload and payload.title) else assessment.title,
        exclude_id=assessment.id,
    )

    if target_status == "published":
        try:
            validate_blueprint_for_publish(
                assessment.test_type,
                assessment.test_config,
                passing_score=assessment.passing_score,
            )
        except AssessmentValidationError as e:
            raise HTTPException(status_code=422, detail=str(e))

    old_title = assessment.title
    now = datetime.now(timezone.utc)
    assessment.title = target_title
    assessment.status = target_status
    assessment.updated_at = now
    with _QCOUNT_LOCK:
        _QCOUNT_CACHE.pop(assessment.id, None)

    reactivated_count = 0
    if target_status == "published" and want_assignments:
        db.flush()
        reactivated_count = reactivate_assignments_for_assessment(db, assessment.id, now=now)

    rename_note = f" (renamed from '{old_title}')" if target_title != old_title else ""
    asg_note = f" and reactivated {reactivated_count} cluster assignment(s)" if reactivated_count else ""
    audit_service.record(
        db,
        actor=current_user,
        action="assessment.restored",
        category="Assessments",
        action_type="Restored",
        entity_type="assessment",
        entity_id=assessment.id,
        entity_name=assessment.title,
        summary=f"Restored archived assessment '{assessment.title}' to {target_status}{rename_note}{asg_note}",
        request=request,
    )

    db.commit()
    db.refresh(assessment)
    return assessment


# ============================================================================
# 3. Candidate Exam Sitting Lifecycle & Proctoring
# ============================================================================

@router.post("/enrollments/{enrollment_id}/attempts", response_model=CandidateAttemptResponse)
def start_or_resume_attempt(
    enrollment_id: int,
    payload: Optional[StartAttemptRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Starts or resumes an attempt inside an atomic transaction with canonical row lock order.
    Live in-progress sittings can always resume (or finalize on timeout) cleanly; new sittings
    require an active published assessment, active assignment, valid deadline, remaining attempts,
    and camera verification when configured.
    """
    if not current_user.employee_id:
        raise HTTPException(status_code=403, detail="User is not linked to an employee record.")

    now = datetime.now(timezone.utc)

    # Step 1: Canonical Lock on assessment_enrollments (of= avoids Postgres outer-join FOR UPDATE error)
    try:
        _set_lock_timeout(db)
        enrollment = (
            db.query(AssessmentEnrollment)
            .options(
                joinedload(AssessmentEnrollment.assessment).defer(Assessment.test_config),
                joinedload(AssessmentEnrollment.assignment),
            )
            .filter(
                AssessmentEnrollment.id == enrollment_id,
                AssessmentEnrollment.employee_id == current_user.employee_id,
            )
            .with_for_update(of=AssessmentEnrollment)
            .first()
        )
    except OperationalError as exc:
        if _is_lock_contention_error(exc):
            db.rollback()
            raise _lock_conflict_http_exception()
        raise
    if not enrollment:
        raise HTTPException(status_code=404, detail="Assessment enrollment not found.")

    assessment = enrollment.assessment
    if not assessment:
        raise HTTPException(status_code=404, detail="Assessment not found.")

    # Step 2: Canonical Lock on live attempt FIRST so an in-progress candidate can always resume/timeout
    try:
        live_attempt = (
            db.query(AssessmentAttempt)
            .filter_by(enrollment_id=enrollment.id, status="in_progress")
            .with_for_update(of=AssessmentAttempt)
            .first()
        )
    except OperationalError as exc:
        if _is_lock_contention_error(exc):
            db.rollback()
            raise _lock_conflict_http_exception()
        raise

    timed_out_prev = False
    if live_attempt:
        exp_at = utc(live_attempt.expires_at) or now
        if exp_at < now:
            finalize_attempt(
                db,
                live_attempt,
                enrollment,
                assessment,
                live_attempt.answers or {},
                now,
                reason="timeout",
            )
            timed_out_prev = True
            if enrollment.status == "passed":
                db.commit()
                raise HTTPException(
                    status_code=409,
                    detail="Your previous sitting timed out and achieved a passing score.",
                )
        else:
            sanitized_snap = sanitize_snapshot_for_candidate(live_attempt.question_snapshot)
            resp = CandidateAttemptResponse.model_validate(live_attempt)
            resp.question_snapshot = sanitized_snap
            return resp

    # New sitting checks (commit lazy timeout finalization before raising errors if timed_out_prev)
    def _reject(code: int, detail: str) -> None:
        if timed_out_prev:
            db.commit()
        raise HTTPException(status_code=code, detail=detail)

    if assessment.status != "published":
        _reject(400, "Assessment is not currently published.")
    if enrollment.status == "cancelled":
        _reject(409, "Enrollment has been cancelled.")
    if enrollment.status == "passed":
        _reject(400, "Candidate has already passed this assessment.")

    # Ensure assignment is still active (repoint to overlapping cluster assignment if needed)
    if enrollment.assignment and not enrollment.assignment.is_active:
        alt = find_alternate_active_assignment(
            db,
            enrollment.employee_id,
            enrollment.assessment_id,
            exclude_assignment_id=enrollment.assignment.id if enrollment.assignment else None,
        )
        if alt:
            src_asg = enrollment.assignment
            enrollment.assignment_id = alt.id
            enrollment.assignment = alt
            if alt.relative_due_days is not None:
                if enrollment.deadline is None:
                    enrollment.deadline = (enrollment.enrolled_at or now) + timedelta(days=alt.relative_due_days)
            else:
                if src_asg and src_asg.relative_due_days is not None:
                    if not enrollment.deadline or (alt.deadline and enrollment.deadline <= alt.deadline):
                        enrollment.deadline = None
            enrollment.updated_at = now
        else:
            _reject(403, "This assessment assignment is no longer active.")

    total_allowed = effective_allowed_attempts(enrollment, assessment)
    attempts_count = (
        db.query(func.count(AssessmentAttempt.id))
        .filter_by(enrollment_id=enrollment.id)
        .scalar()
        or 0
    )
    if attempts_count >= total_allowed:
        _reject(403, f"All {total_allowed} allowed attempt(s) have been exhausted for this assessment.")

    eff_dl = _effective_deadline(enrollment)
    if eff_dl and eff_dl < now:
        _reject(403, "The deadline for this assessment has passed.")

    proctoring_cfg = assessment.proctoring_config or {}
    camera_verified = payload.camera_verified if payload else False
    if proctoring_cfg.get("require_camera") and not camera_verified:
        _reject(400, "Camera verification is required before starting this assessment.")

    try:
        fresh_snapshot = generate_candidate_exam_paper(
            assessment.test_type,
            assessment.test_config,
            assessment.passing_score,
            proctoring_config=proctoring_cfg,
        )
    except ValueError as e:
        _reject(422, str(e))

    expires_at = now + timedelta(minutes=assessment.duration_minutes, seconds=60)

    new_attempt = AssessmentAttempt(
        enrollment_id=enrollment.id,
        attempt_number=attempts_count + 1,
        assessment_version=assessment.version,
        status="in_progress",
        question_snapshot=fresh_snapshot,
        started_at=now,
        expires_at=expires_at,
        camera_verified=camera_verified,
        answers={},
        answers_seq=0,
    )
    db.add(new_attempt)
    enrollment.status = "in_progress"
    enrollment.updated_at = now
    db.flush()

    sanitized_snap = sanitize_snapshot_for_candidate(fresh_snapshot)
    resp = CandidateAttemptResponse(
        id=new_attempt.id,
        enrollment_id=new_attempt.enrollment_id,
        attempt_number=new_attempt.attempt_number,
        assessment_version=new_attempt.assessment_version,
        status=new_attempt.status,
        question_snapshot=sanitized_snap,
        started_at=new_attempt.started_at,
        expires_at=new_attempt.expires_at,
        answers={},
        answers_seq=0,
        camera_verified=bool(new_attempt.camera_verified),
    )
    db.commit()
    return resp


@router.put("/attempts/{attempt_id}/draft", status_code=status.HTTP_200_OK)
def autosave_draft(
    attempt_id: int,
    payload: DraftAutosaveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Monotonic draft autosave endpoint guarded with FOR UPDATE row lock.
    Returns saved=False when a stale/duplicate answers_seq is received so telemetry beacons
    can use POST /attempts/{attempt_id}/telemetry without coupling to answer changes.
    """
    if not current_user.employee_id:
        raise HTTPException(status_code=403, detail="Unauthorized.")

    now = datetime.now(timezone.utc)
    try:
        _set_lock_timeout(db)
        attempt = (
            db.query(AssessmentAttempt)
            .options(defer(AssessmentAttempt.result_summary))
            .join(AssessmentEnrollment, AssessmentEnrollment.id == AssessmentAttempt.enrollment_id)
            .filter(
                AssessmentAttempt.id == attempt_id,
                AssessmentEnrollment.employee_id == current_user.employee_id,
            )
            .with_for_update(of=AssessmentAttempt, nowait=True)
            .first()
        )
    except OperationalError as exc:
        if _is_lock_contention_error(exc):
            db.rollback()
            raise _lock_conflict_http_exception(
                "Concurrent draft autosave is already in progress; retry with the latest sequence."
            )
        raise
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found.")
    if attempt.status != "in_progress":
        raise HTTPException(status_code=409, detail=f"Attempt is already {attempt.status}.")
    if (utc(attempt.expires_at) or now) < now:
        raise HTTPException(status_code=409, detail="Attempt has expired.")

    try:
        validate_draft_payload(attempt.question_snapshot, {"answers": payload.answers})
    except AssessmentValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    events_payload = [ev.model_dump() for ev in (payload.events or [])]
    if events_payload:
        _ingest_telemetry_events(db, attempt.id, events_payload, now)

    should_save = bool(
        payload.answers_seq > attempt.answers_seq
        or (attempt.answers_seq == 0 and not attempt.answers and bool(payload.answers))
    )

    if should_save:
        test_type = attempt.question_snapshot.get("test_type", "mcq")
        new_answers = dict(payload.answers)
        if test_type == "typing":
            passage_len = len(str((attempt.question_snapshot or {}).get("passage_text") or ""))
            max_keys = max(passage_len * 10, 10000)
            stored_keys = int((attempt.answers or {}).get("keystroke_count") or 0)
            incoming_keys = int(new_answers.get("keystroke_count") or 0)
            new_answers["keystroke_count"] = min(max(stored_keys, incoming_keys), max_keys)

        attempt.answers = new_answers
        attempt.answers_seq = max(payload.answers_seq, attempt.answers_seq + 1 if attempt.answers_seq == 0 else payload.answers_seq)
        attempt.updated_at = now
        flag_modified(attempt, "answers")

        db.commit()
        return {"detail": "Draft saved successfully.", "answers_seq": attempt.answers_seq, "saved": True}

    if events_payload:
        db.commit()

    return {
        "detail": "Stale draft ignored; newer sequence already saved.",
        "answers_seq": attempt.answers_seq,
        "saved": False,
    }


@router.post("/attempts/{attempt_id}/telemetry", status_code=status.HTTP_200_OK)
def ingest_attempt_telemetry(
    attempt_id: int,
    payload: TelemetryIngestRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Ingests batched proctoring telemetry events independently of answer changes
    using a lock-free scalar status/expiry check so beacons never queue behind autosave locks.
    """
    if not current_user.employee_id:
        raise HTTPException(status_code=403, detail="Unauthorized.")

    now = datetime.now(timezone.utc)
    row = (
        db.query(AssessmentAttempt.id, AssessmentAttempt.status, AssessmentAttempt.expires_at)
        .join(AssessmentEnrollment, AssessmentEnrollment.id == AssessmentAttempt.enrollment_id)
        .filter(
            AssessmentAttempt.id == attempt_id,
            AssessmentEnrollment.employee_id == current_user.employee_id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Attempt not found.")
    att_id, att_status, att_expires_at = row
    if att_status != "in_progress":
        raise HTTPException(status_code=409, detail=f"Attempt is already {att_status}.")
    if (utc(att_expires_at) or now) < now:
        raise HTTPException(status_code=409, detail="Attempt has expired.")

    recorded = _ingest_telemetry_events(
        db,
        att_id,
        [ev.model_dump() for ev in payload.events],
        now,
    )
    db.commit()
    return {"detail": "Telemetry recorded.", "recorded_count": recorded}


@router.post("/attempts/{attempt_id}/submit", response_model=CandidateAttemptResultResponse)
def submit_attempt(
    attempt_id: int,
    payload: SubmitAttemptRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Submits attempt for server-side evaluation.
    Canonical lock order: locks enrollment, then attempt.
    Strips question diagnostics and anti-cheat flag indicators from candidate response.
    """
    if not current_user.employee_id:
        raise HTTPException(status_code=403, detail="Unauthorized.")

    now = datetime.now(timezone.utc)

    att_parent = db.query(AssessmentAttempt.enrollment_id).filter_by(id=attempt_id).first()
    if not att_parent:
        raise HTTPException(status_code=404, detail="Attempt not found.")

    try:
        _set_lock_timeout(db)
        enrollment = (
            db.query(AssessmentEnrollment)
            .options(joinedload(AssessmentEnrollment.assessment).defer(Assessment.test_config))
            .filter(
                AssessmentEnrollment.id == att_parent[0],
                AssessmentEnrollment.employee_id == current_user.employee_id,
            )
            .with_for_update(of=AssessmentEnrollment)
            .first()
        )
        if not enrollment:
            raise HTTPException(status_code=404, detail="Enrollment not found or unauthorized.")

        attempt = (
            db.query(AssessmentAttempt)
            .filter_by(id=attempt_id)
            .with_for_update(of=AssessmentAttempt)
            .first()
        )
    except OperationalError as exc:
        if _is_lock_contention_error(exc):
            db.rollback()
            raise _lock_conflict_http_exception()
        raise
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found.")

    if attempt.status != "in_progress":
        raise HTTPException(
            status_code=409,
            detail=f"Attempt is already {attempt.status} and cannot be submitted again.",
        )

    exp_at = utc(attempt.expires_at) or now
    is_expired = now > exp_at

    if is_expired:
        finalize_attempt(
            db,
            attempt,
            enrollment,
            enrollment.assessment,
            attempt.answers or {},
            now,
            reason="timeout",
        )
    else:
        submitted = dict(payload.answers or {})
        try:
            validate_draft_payload(attempt.question_snapshot, {"answers": submitted})
        except AssessmentValidationError as e:
            raise HTTPException(status_code=422, detail=str(e))

        test_type = attempt.question_snapshot.get("test_type", "mcq")
        if test_type == "typing":
            passage_len = len(str((attempt.question_snapshot or {}).get("passage_text") or ""))
            max_keys = max(passage_len * 10, 10000)
            draft_keys = int((attempt.answers or {}).get("keystroke_count") or 0)
            submit_keys = int(submitted.get("keystroke_count") or 0)
            submitted["keystroke_count"] = min(max(draft_keys, submit_keys), max_keys)

        finalize_attempt(
            db,
            attempt,
            enrollment,
            enrollment.assessment,
            submitted,
            now,
            reason="candidate_submit",
        )

    resp = _to_candidate_attempt_result(attempt)
    db.commit()
    return resp


@router.post("/enrollments/{enrollment_id}/grant-retest", status_code=status.HTTP_200_OK)
def grant_retest(
    enrollment_id: int,
    payload: GrantRetestRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """
    Admin/HR only: Grants an additional sitting to a candidate.
    Requires future deadline if the candidate's current deadline has already passed.
    """
    now = datetime.now(timezone.utc)
    try:
        _set_lock_timeout(db)
        enrollment = (
            db.query(AssessmentEnrollment)
            .options(
                joinedload(AssessmentEnrollment.employee).load_only(
                    Employee.id, Employee.name, Employee.email, Employee.designation
                ),
                joinedload(AssessmentEnrollment.assignment),
                joinedload(AssessmentEnrollment.assessment).defer(Assessment.test_config),
            )
            .filter_by(id=enrollment_id)
            .with_for_update(of=AssessmentEnrollment)
            .first()
        )
    except OperationalError as exc:
        if _is_lock_contention_error(exc):
            db.rollback()
            raise _lock_conflict_http_exception()
        raise
    if not enrollment:
        raise HTTPException(status_code=404, detail="Enrollment not found.")

    if not enrollment.assessment or enrollment.assessment.status != "published":
        raise HTTPException(
            status_code=400,
            detail="Cannot grant retest for an assessment that is not currently published.",
        )
    if not enrollment.assignment or not enrollment.assignment.is_active:
        old_asg = enrollment.assignment
        alt = find_alternate_active_assignment(
            db,
            enrollment.employee_id,
            enrollment.assessment_id,
            exclude_assignment_id=old_asg.id if old_asg else None,
        )
        if not alt:
            raise HTTPException(
                status_code=400,
                detail="Cannot grant retest because the assignment is no longer active and no alternate active cohort was found.",
            )
        enrollment.assignment_id = alt.id
        enrollment.assignment = alt
        if alt.relative_due_days is not None:
            if enrollment.deadline is None:
                enrollment.deadline = now + timedelta(days=alt.relative_due_days)
        elif old_asg and old_asg.relative_due_days is not None:
            if alt.deadline and enrollment.deadline and enrollment.deadline <= alt.deadline:
                enrollment.deadline = None
        elif alt.deadline and enrollment.deadline and enrollment.deadline <= alt.deadline:
            enrollment.deadline = None

    if enrollment.status == "passed":
        raise HTTPException(status_code=400, detail="Cannot grant retest for an already passed assessment.")
    if enrollment.status == "in_progress":
        raise HTTPException(status_code=409, detail="Candidate currently has an active attempt in progress.")
    if enrollment.status == "cancelled":
        raise HTTPException(status_code=409, detail="Cannot grant retest on a cancelled enrollment.")

    eff_dl = _effective_deadline(enrollment)
    if payload.new_deadline is not None:
        new_dl = utc(payload.new_deadline)
        if not new_dl or new_dl <= now:
            raise HTTPException(status_code=400, detail="New deadline must be a future datetime.")
        if eff_dl and new_dl <= eff_dl:
            raise HTTPException(
                status_code=400,
                detail="New deadline must be later than the current effective deadline (Never Shorten rule).",
            )
        enrollment.deadline = new_dl
    elif eff_dl and eff_dl <= now:
        raise HTTPException(
            status_code=400,
            detail="Candidate's deadline has already passed; provide a future new_deadline when granting retest.",
        )

    enrollment.extra_attempts += 1
    enrollment.status = "assigned"
    enrollment.updated_at = now

    audit_service.record(
        db,
        actor=current_user,
        action="assessment.retest_granted",
        category="Assessments",
        action_type="Updated",
        entity_type="assessment_enrollment",
        entity_id=enrollment.id,
        subject_employee_id=enrollment.employee_id,
        subject_name=enrollment.employee.name if enrollment.employee else None,
        summary=f"Granted retest sitting. Reason: {payload.reason}",
        request=request,
    )

    db.commit()
    return {
        "detail": "Retest sitting granted successfully.",
        "extra_attempts": enrollment.extra_attempts,
        "status": enrollment.status,
        "deadline": _effective_deadline(enrollment),
    }


@router.put("/enrollments/{enrollment_id}/extend-deadline", status_code=status.HTTP_200_OK)
def extend_candidate_deadline(
    enrollment_id: int,
    payload: CandidateDeadlineExtendRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """Admin/HR only: Grants individual candidate deadline extension (Never Shorten enforced)."""
    now = datetime.now(timezone.utc)
    try:
        _set_lock_timeout(db)
        enrollment = (
            db.query(AssessmentEnrollment)
            .options(
                joinedload(AssessmentEnrollment.employee),
                joinedload(AssessmentEnrollment.assignment),
            )
            .filter_by(id=enrollment_id)
            .with_for_update(of=AssessmentEnrollment)
            .first()
        )
    except OperationalError as exc:
        if _is_lock_contention_error(exc):
            db.rollback()
            raise _lock_conflict_http_exception()
        raise
    if not enrollment:
        raise HTTPException(status_code=404, detail="Enrollment not found.")

    if enrollment.status in ("cancelled", "passed"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot extend deadline on a {enrollment.status} enrollment.",
        )

    new_dl = utc(payload.new_deadline)
    if not new_dl or new_dl <= now:
        raise HTTPException(status_code=400, detail="New deadline must be a future datetime.")

    eff_dl = _effective_deadline(enrollment)
    if eff_dl and new_dl <= eff_dl:
        raise HTTPException(
            status_code=400,
            detail="New deadline must be strictly later than the current effective deadline (Never Shorten rule).",
        )

    old_deadline = eff_dl
    enrollment.deadline = new_dl
    enrollment.updated_at = now

    audit_service.record(
        db,
        actor=current_user,
        action="assessment.deadline_extended",
        category="Assessments",
        action_type="Updated",
        entity_type="assessment_enrollment",
        entity_id=enrollment.id,
        subject_employee_id=enrollment.employee_id,
        subject_name=enrollment.employee.name if enrollment.employee else None,
        summary=f"Extended candidate deadline from {old_deadline} to {new_dl}. Reason: {payload.reason}",
        request=request,
    )

    db.commit()
    return {
        "detail": "Candidate deadline extended successfully.",
        "deadline": enrollment.deadline,
    }


# ============================================================================
# 4. Proctoring Video Uploads, Retention & Scoped Playback / Logs
# ============================================================================

@router.post("/attempts/{attempt_id}/recording/upload-url", status_code=status.HTTP_200_OK)
def get_recording_upload_url(
    attempt_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Generates one-shot pre-signed upload URL for webcam proctoring recording.
    Refuses new URLs once finalized/purged/failed, and enforces the 30-minute post-submit window.
    """
    if not current_user.employee_id:
        raise HTTPException(status_code=403, detail="Unauthorized.")

    now = datetime.now(timezone.utc)
    att_row = (
        db.query(AssessmentAttempt.id, AssessmentAttempt.status, AssessmentAttempt.submitted_at)
        .join(AssessmentEnrollment, AssessmentEnrollment.id == AssessmentAttempt.enrollment_id)
        .filter(
            AssessmentAttempt.id == attempt_id,
            AssessmentEnrollment.employee_id == current_user.employee_id,
        )
        .first()
    )
    if not att_row:
        raise HTTPException(status_code=404, detail="Attempt not found.")
    att_id, att_status, att_submitted_at = att_row

    if att_status != "in_progress":
        submitted_at = utc(att_submitted_at) or now
        if now - submitted_at > timedelta(minutes=30):
            raise HTTPException(status_code=403, detail="Proctoring video upload window has expired.")

    canonical_path = f"assessments/{att_id}/proctoring.webm"

    rec_status = db.query(AssessmentRecording.status).filter_by(attempt_id=att_id).scalar()
    if rec_status is not None:
        if rec_status == "purged":
            raise HTTPException(status_code=410, detail="Recording has already been purged.")
        if rec_status == "ready":
            raise HTTPException(status_code=409, detail="Recording has already been finalized and cannot be overwritten.")
        if rec_status == "failed":
            raise HTTPException(status_code=409, detail="Recording upload window failed and can no longer be uploaded.")
    else:
        rec = AssessmentRecording(
            attempt_id=att_id,
            video_storage_path=canonical_path,
            status="pending_upload",
            expires_at=now + timedelta(days=7),
        )
        db.add(rec)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            rec_status = db.query(AssessmentRecording.status).filter_by(attempt_id=att_id).scalar()
            if rec_status != "pending_upload":
                raise HTTPException(status_code=409, detail="Recording upload state conflict.")

    # Release DB connection before external Supabase Storage signed-URL call
    db.close()

    try:
        upload_url = create_signed_upload_url(att_id, canonical_path, expires_in=900)
    except RuntimeError as exc:
        logger.error("[get_recording_upload_url] Storage error for attempt %s: %s", att_id, exc)
        status_code = 503 if not is_supabase_configured() and not is_local_fallback_allowed() else 502
        raise HTTPException(status_code=status_code, detail="Could not issue proctoring recording upload URL.")

    return {
        "upload_url": upload_url,
        "video_storage_path": canonical_path,
        "expires_in_minutes": 15,
    }


@router.put("/attempts/{attempt_id}/recording/local", status_code=status.HTTP_200_OK)
async def upload_local_recording_fallback(
    attempt_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Local dev/test storage fallback streamed to disk without blocking the event loop or holding DB locks."""
    if is_supabase_configured() or not is_local_fallback_allowed():
        raise HTTPException(status_code=404, detail="Local storage upload is disabled.")
    if not current_user.employee_id:
        raise HTTPException(status_code=403, detail="Unauthorized.")

    now = datetime.now(timezone.utc)

    def _load_meta() -> tuple[int, str]:
        try:
            row = (
                db.query(
                    AssessmentRecording.id,
                    AssessmentRecording.status,
                    AssessmentRecording.video_storage_path,
                    AssessmentAttempt.status,
                    AssessmentAttempt.submitted_at,
                )
                .join(AssessmentAttempt, AssessmentAttempt.id == AssessmentRecording.attempt_id)
                .join(AssessmentEnrollment, AssessmentEnrollment.id == AssessmentAttempt.enrollment_id)
                .filter(
                    AssessmentRecording.attempt_id == attempt_id,
                    AssessmentEnrollment.employee_id == current_user.employee_id,
                )
                .first()
            )
            if not row:
                raise HTTPException(status_code=404, detail="Recording record not found.")
            r_id, r_status, r_path, a_status, a_submitted_at = row
            if r_status == "purged":
                raise HTTPException(status_code=410, detail="Recording has already been purged.")
            if r_status != "pending_upload":
                raise HTTPException(status_code=409, detail=f"Recording is already {r_status}.")
            if a_status != "in_progress":
                submitted_at = utc(a_submitted_at) or now
                if now - submitted_at > timedelta(minutes=30):
                    raise HTTPException(status_code=403, detail="Proctoring video upload window has expired.")
            return int(r_id), str(r_path)
        finally:
            db.close()

    rec_id, video_storage_path = await anyio.to_thread.run_sync(_load_meta)

    content_len = request.headers.get("content-length")
    if content_len and content_len.isdigit() and int(content_len) > MAX_LOCAL_RECORDING_BYTES:
        raise HTTPException(status_code=413, detail="Recording exceeds maximum allowed size.")

    full_path = os.path.join(LOCAL_RECORDINGS_DIR, video_storage_path)
    target_dir = os.path.dirname(full_path)
    os.makedirs(target_dir, exist_ok=True)

    total_bytes = 0
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                total_bytes += len(chunk)
                if total_bytes > MAX_LOCAL_RECORDING_BYTES:
                    raise HTTPException(status_code=413, detail="Recording exceeds maximum allowed size.")
                f.write(chunk)
        if total_bytes == 0:
            raise HTTPException(status_code=400, detail="Recording upload cannot be empty.")
        os.replace(tmp_path, full_path)
        tmp_path = None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _persist_size() -> None:
        try:
            db.query(AssessmentRecording).filter_by(id=rec_id).update(
                {"file_size_bytes": total_bytes, "updated_at": now},
                synchronize_session=False,
            )
            db.commit()
        finally:
            db.close()

    await anyio.to_thread.run_sync(_persist_size)
    return {"detail": "Recording uploaded to local fallback storage.", "file_size_bytes": total_bytes}


@router.get("/attempts/{attempt_id}/recording/local")
def play_local_recording_fallback(
    attempt_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_viewer),
):
    """Scoped evaluator local dev storage playback fallback."""
    if is_supabase_configured() or not is_local_fallback_allowed():
        raise HTTPException(status_code=404, detail="Local storage playback is disabled.")

    now = datetime.now(timezone.utc)
    rec_row = (
        db.query(
            AssessmentRecording.status,
            AssessmentRecording.expires_at,
            AssessmentRecording.video_storage_path,
            AssessmentEnrollment.employee_id,
        )
        .join(AssessmentAttempt, AssessmentAttempt.id == AssessmentRecording.attempt_id)
        .join(AssessmentEnrollment, AssessmentEnrollment.id == AssessmentAttempt.enrollment_id)
        .filter(AssessmentRecording.attempt_id == attempt_id)
        .first()
    )
    if not rec_row:
        raise HTTPException(status_code=404, detail="Recording not found.")

    rec_status, rec_expires_at, video_storage_path, target_emp_id = rec_row
    _ensure_evaluator_can_view_employee(db, current_user, target_emp_id)

    if rec_status == "purged" or (rec_expires_at and utc(rec_expires_at) <= now):
        raise HTTPException(status_code=410, detail="Recording has expired and been purged.")
    if rec_status != "ready":
        raise HTTPException(status_code=409, detail=f"Recording is not ready for playback (status={rec_status}).")

    full_path = os.path.join(LOCAL_RECORDINGS_DIR, video_storage_path)
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="Local recording file not found.")

    db.close()
    return FileResponse(full_path, media_type="video/webm")


@router.post("/attempts/{attempt_id}/recording/finalize", status_code=status.HTTP_200_OK)
def finalize_recording_upload(
    attempt_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Marks recording uploaded and anchors 7-day TTL.
    Enforces pending_upload status, 30-min post-submit window, and storage existence verification.
    """
    if not current_user.employee_id:
        raise HTTPException(status_code=403, detail="Unauthorized.")

    now = datetime.now(timezone.utc)
    rec_row = (
        db.query(
            AssessmentRecording.id,
            AssessmentRecording.status,
            AssessmentRecording.video_storage_path,
            AssessmentAttempt.status,
            AssessmentAttempt.submitted_at,
        )
        .join(AssessmentAttempt, AssessmentAttempt.id == AssessmentRecording.attempt_id)
        .join(AssessmentEnrollment, AssessmentEnrollment.id == AssessmentAttempt.enrollment_id)
        .filter(
            AssessmentRecording.attempt_id == attempt_id,
            AssessmentEnrollment.employee_id == current_user.employee_id,
        )
        .first()
    )
    if not rec_row:
        raise HTTPException(status_code=404, detail="Recording record not found.")

    rec_id, rec_status, video_path, att_status, att_submitted_at = rec_row
    if rec_status == "purged":
        raise HTTPException(status_code=410, detail="Recording has already been purged.")
    if rec_status != "pending_upload":
        raise HTTPException(status_code=409, detail=f"Recording is already {rec_status}.")

    if att_status != "in_progress":
        submitted_at = utc(att_submitted_at) or now
        if now - submitted_at > timedelta(minutes=30):
            raise HTTPException(status_code=403, detail="Proctoring video upload window has expired.")

    # Release DB connection BEFORE calling external Supabase Storage API
    db.close()

    exists = check_recording_exists(video_path)
    if exists is False:
        raise HTTPException(status_code=400, detail="Recording object has not been uploaded to storage yet.")
    if exists is None:
        raise HTTPException(status_code=502, detail="Could not verify uploaded recording in storage.")

    now_finalize = datetime.now(timezone.utc)
    try:
        _set_lock_timeout(db)
        rec_locked = (
            db.query(AssessmentRecording)
            .filter_by(id=rec_id)
            .with_for_update(of=AssessmentRecording)
            .first()
        )
    except OperationalError as exc:
        if _is_lock_contention_error(exc):
            db.rollback()
            raise _lock_conflict_http_exception()
        raise
    if not rec_locked:
        raise HTTPException(status_code=404, detail="Recording record not found.")
    if rec_locked.status == "purged":
        raise HTTPException(status_code=410, detail="Recording has already been purged.")
    if rec_locked.status != "pending_upload":
        raise HTTPException(status_code=409, detail=f"Recording is already {rec_locked.status}.")

    rec_locked.status = "ready"
    rec_locked.expires_at = now_finalize + timedelta(days=7)
    rec_locked.updated_at = now_finalize
    db.commit()
    return {"detail": "Recording finalized successfully.", "expires_at": utc(rec_locked.expires_at)}


@router.get("/results/{attempt_id}/recording/play-url", status_code=status.HTTP_200_OK)
def get_recording_play_url(
    attempt_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_viewer),
):
    """
    Scoped Evaluator only: Returns signed playback URL if recording is ready and not expired/purged.
    Admin & HR see all; PM & Team Lead see only manageable team members (never themselves or peers).
    """
    now = datetime.now(timezone.utc)
    rec_row = (
        db.query(
            AssessmentRecording.status,
            AssessmentRecording.expires_at,
            AssessmentRecording.video_storage_path,
            AssessmentRecording.file_size_bytes,
            AssessmentEnrollment.employee_id,
        )
        .join(AssessmentAttempt, AssessmentAttempt.id == AssessmentRecording.attempt_id)
        .join(AssessmentEnrollment, AssessmentEnrollment.id == AssessmentAttempt.enrollment_id)
        .filter(AssessmentRecording.attempt_id == attempt_id)
        .first()
    )
    if not rec_row:
        raise HTTPException(status_code=404, detail="Recording not found.")

    rec_status, rec_expires_at, video_path, file_size_bytes, target_emp_id = rec_row
    _ensure_evaluator_can_view_employee(db, current_user, target_emp_id)

    if rec_status == "purged" or (rec_expires_at and utc(rec_expires_at) <= now):
        raise HTTPException(status_code=410, detail="Recording has expired and been purged.")
    if rec_status != "ready":
        raise HTTPException(status_code=409, detail=f"Recording is not ready for playback (status={rec_status}).")

    # Release DB connection before external Supabase Storage call
    db.close()

    play_url = create_signed_play_url(attempt_id, video_path, expires_in=1800)
    if not play_url:
        raise HTTPException(status_code=502, detail="Could not generate playback URL.")

    return {
        "play_url": play_url,
        "video_storage_path": video_path,
        "file_size_bytes": file_size_bytes,
        "expires_at": utc(rec_expires_at),
        "expires_in_minutes": 30,
    }


@router.get("/results/{attempt_id}/logs", response_model=List[AssessmentLogResponse])
def get_attempt_proctoring_logs(
    attempt_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_viewer),
):
    """
    Scoped Evaluator only: Returns chronological proctoring events for an attempt.
    Admin & HR see all; PM & Team Lead see only manageable team members (never themselves or peers).
    """
    emp_row = (
        db.query(AssessmentEnrollment.employee_id)
        .join(AssessmentAttempt, AssessmentAttempt.enrollment_id == AssessmentEnrollment.id)
        .filter(AssessmentAttempt.id == attempt_id)
        .first()
    )
    if not emp_row:
        raise HTTPException(status_code=404, detail="Attempt not found.")

    _ensure_evaluator_can_view_employee(db, current_user, emp_row[0])

    return (
        db.query(AssessmentLog)
        .filter_by(attempt_id=attempt_id)
        .order_by(AssessmentLog.event_time.asc(), AssessmentLog.id.asc())
        .all()
    )


@router.get(
    "/enrollments/{enrollment_id}/scorecard",
    response_model=Union[ScorecardResponse, CandidateScorecardResponse],
)
def get_scorecard(
    enrollment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Scorecard view:
    - Admin/HR: full evaluator diagnostics across all employees.
    - PM/Team Lead: full evaluator diagnostics ONLY for subordinate team members in project_scope;
      candidate-safe view on their own enrollment; 403 for peers or other teams.
    - Employee: candidate-safe view on their own enrollment only.
    """
    enrollment = (
        db.query(AssessmentEnrollment)
        .options(
            joinedload(AssessmentEnrollment.assessment).defer(Assessment.test_config),
            joinedload(AssessmentEnrollment.assignment).joinedload(Assignment.cluster),
            joinedload(AssessmentEnrollment.employee).load_only(
                Employee.id, Employee.name, Employee.email, Employee.designation, Employee.avatar_url
            ),
            selectinload(AssessmentEnrollment.attempts)
            .defer(AssessmentAttempt.answers),
        )
        .filter_by(id=enrollment_id)
        .first()
    )
    if not enrollment:
        raise HTTPException(status_code=404, detail="Enrollment not found.")

    emp_name = enrollment.employee.name if enrollment.employee else None

    def _evaluator_view() -> ScorecardResponse:
        att_ids = [att.id for att in (enrollment.attempts or [])]
        resp = _build_evaluator_scorecard(
            enrollment,
            emp_name,
            event_counts_by_attempt=_fetch_attempt_event_counts(db, att_ids),
            recording_status_by_attempt=_fetch_recording_statuses(db, att_ids),
            logs_by_attempt=_fetch_attempt_logs(db, att_ids),
        )
        if enrollment.assignment and enrollment.assignment.cluster:
            resp.cluster_name = enrollment.assignment.cluster.name
        return resp

    if current_user.role in ("admin", "hr"):
        return _evaluator_view()

    if current_user.role in ("pm", "team_lead"):
        if current_user.employee_id and enrollment.employee_id == current_user.employee_id:
            return _build_candidate_scorecard(
                enrollment, emp_name, _question_counts(db, [enrollment.assessment]).get(enrollment.assessment_id)
            )
        scoped_ids = _get_scoped_evaluator_emp_ids(db, current_user)
        if scoped_ids is None or enrollment.employee_id in scoped_ids:
            return _evaluator_view()
        raise HTTPException(status_code=403, detail="Access denied.")

    if enrollment.employee_id != current_user.employee_id:
        raise HTTPException(status_code=403, detail="Access denied.")

    return _build_candidate_scorecard(
        enrollment, emp_name, _question_counts(db, [enrollment.assessment]).get(enrollment.assessment_id)
    )
