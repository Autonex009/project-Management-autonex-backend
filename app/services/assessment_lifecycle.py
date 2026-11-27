from datetime import datetime, timezone
from typing import Any, Dict, Optional
from sqlalchemy import case
from sqlalchemy.sql import func
from app.services.assessment_engine import evaluate_candidate_attempt


def utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalizes naive datetimes (e.g. from SQLite) to UTC-aware datetimes."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def effective_allowed_attempts(enrollment: Any, assessment: Any) -> int:
    """Computes total allowed attempts without retroactively penalizing blueprint reductions."""
    base_max = max(int(assessment.max_attempts or 1), int(getattr(enrollment, "max_attempts_snapshot", None) or 0))
    return base_max + int(enrollment.extra_attempts or 0)


def finalize_attempt(
    db: Any,
    attempt: Any,
    enrollment: Any,
    assessment: Any,
    answers: Optional[Dict[str, Any]],
    now: datetime,
    reason: str,
) -> None:
    """
    Single authoritative finalization path.
    reason: "candidate_submit" -> attempt.status = 'completed'
            "timeout"          -> attempt.status = 'abandoned' (graded on saved draft)
    The caller must already hold the enrollment -> attempt row locks.
    """
    from app.models.assessment import AssessmentAttempt, AssessmentLog

    answers = dict(answers or {})
    start_dt = utc(attempt.started_at) or now
    exp_dt = utc(attempt.expires_at) or now
    finish_time = min(exp_dt, now)
    elapsed_seconds = max((finish_time - start_dt).total_seconds(), 1.0)

    # On timeout, cap typing evaluation elapsed time to scheduled duration so the 60s grace window
    # does not artificially penalize WPM.
    eval_elapsed = elapsed_seconds
    if reason == "timeout" and assessment and getattr(assessment, "duration_minutes", None):
        eval_elapsed = min(elapsed_seconds, float(assessment.duration_minutes) * 60.0)
        eval_elapsed = max(eval_elapsed, 1.0)

    score, passed, result_summary = evaluate_candidate_attempt(
        attempt.question_snapshot, answers, time_spent_seconds=eval_elapsed
    )
    result_summary = dict(result_summary)
    result_summary["auto_submitted"] = (reason == "timeout")
    result_summary["reason"] = reason

    # Enforce proctoring tab-switch threshold server-side against frozen/current config
    proctoring_cfg = (attempt.question_snapshot or {}).get("proctoring_config")
    if not proctoring_cfg and assessment is not None:
        proctoring_cfg = assessment.proctoring_config or {}
    proctoring_cfg = proctoring_cfg or {}

    flag_reasons = []
    if result_summary.get("is_flagged") and result_summary.get("flag_reason"):
        flag_reasons.append(str(result_summary["flag_reason"]))

    max_tabs = proctoring_cfg.get("max_tab_switches")
    if max_tabs is not None and not isinstance(max_tabs, bool):
        try:
            max_tabs_int = int(max_tabs)
            tab_switches = (
                db.query(func.count(AssessmentLog.id))
                .filter(
                    AssessmentLog.attempt_id == attempt.id,
                    AssessmentLog.event_type == "tab_switch",
                )
                .scalar()
                or 0
            )
            if tab_switches > max_tabs_int:
                flag_reasons.append(f"tab_switches_exceeded ({tab_switches}>{max_tabs_int})")
        except (ValueError, TypeError):
            pass

    attempt.status = "abandoned" if reason == "timeout" else "completed"
    attempt.submitted_at = finish_time
    attempt.answers = answers
    attempt.result_summary = result_summary
    attempt.score = score
    attempt.passed = passed
    if flag_reasons:
        attempt.is_flagged = True
        attempt.flag_reason = "; ".join(flag_reasons)

    db.flush()

    total_allowed = effective_allowed_attempts(enrollment, assessment)
    agg_row = (
        db.query(
            func.count(AssessmentAttempt.id),
            func.coalesce(
                func.sum(case((AssessmentAttempt.passed == True, 1), else_=0)),
                0,
            ),
        )
        .filter(AssessmentAttempt.enrollment_id == enrollment.id)
        .first()
    )
    attempts_count = int(agg_row[0] or 0) if agg_row else 0
    has_passed = passed or bool(agg_row and int(agg_row[1] or 0) > 0)

    if has_passed:
        enrollment.status = "passed"
    elif attempts_count < total_allowed:
        enrollment.status = "assigned"
    else:
        enrollment.status = "failed"
    enrollment.updated_at = now
