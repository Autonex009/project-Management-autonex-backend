"""
Standalone PostgreSQL integration test harness executed in GitHub Actions CI against postgres:16.
Subcommands:
  bootstrap  Creates pre-Feature-04 base tables and stamps Alembic at g1h2i3j4k5l6 so
             alembic upgrade head builds all Feature-04 tables, JSONB columns & partial indexes.
  verify     Verifies real PostgreSQL DDL, JSONB columns, partial indexes, SET LOCAL lock_timeout,
             FOR UPDATE OF ... NOWAIT, and SQLSTATE 55P03 lock contention behavior.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import joinedload, sessionmaker

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app.models.allocation       # noqa: F401
import app.models.audit_log        # noqa: F401
import app.models.employee         # noqa: F401
import app.models.guideline        # noqa: F401
import app.models.leave            # noqa: F401
import app.models.notification     # noqa: F401
import app.models.parent_project   # noqa: F401
import app.models.payroll          # noqa: F401
import app.models.perf_eval        # noqa: F401
import app.models.project          # noqa: F401
import app.models.referral         # noqa: F401
import app.models.side_project     # noqa: F401
import app.models.signup_request   # noqa: F401
import app.models.skill            # noqa: F401
import app.models.sub_project      # noqa: F401
import app.models.user             # noqa: F401
import app.models.weekly_compensation  # noqa: F401
import app.models.wfh              # noqa: F401
import app.models.cluster          # noqa: F401
import app.models.assignment       # noqa: F401
import app.models.assessment       # noqa: F401

from app.api.assessments import _is_lock_contention_error, _set_lock_timeout
from app.db.database import Base
from app.models.assessment import Assessment, AssessmentAttempt, AssessmentEnrollment
from app.models.employee import Employee

FEATURE04_TABLES = {
    "clusters",
    "cluster_members",
    "assignments",
    "assessments",
    "assessment_enrollments",
    "assessment_attempts",
    "assessment_logs",
    "assessment_recordings",
}


def _ensure_safe_test_target(db_url: str) -> None:
    """Prevent accidentally seeding or altering a non-CI remote production database."""
    from sqlalchemy.engine.url import make_url

    host = (make_url(db_url).host or "").lower()
    is_local = host in ("localhost", "127.0.0.1", "postgres", "db", "")
    is_ci = os.getenv("CI", "").lower() == "true"
    allow_override = os.getenv("ALLOW_PG_INTEGRATION_ON_REMOTE", "").lower() == "true"
    if not (is_local or is_ci or allow_override):
        raise RuntimeError(
            f"Refusing to run postgres_integration_check against non-local host '{host}'. "
            "Set CI=true or ALLOW_PG_INTEGRATION_ON_REMOTE=true if intentional."
        )


def bootstrap() -> None:
    db_url = os.environ["DATABASE_URL"]
    _ensure_safe_test_target(db_url)
    engine = create_engine(db_url, pool_pre_ping=True)
    base_tables = [t for name, t in Base.metadata.tables.items() if name not in FEATURE04_TABLES]
    Base.metadata.create_all(engine, tables=base_tables)
    engine.dispose()
    print(f"Bootstrapped {len(base_tables)} pre-Feature-04 base tables.")


def verify() -> None:
    db_url = os.environ["DATABASE_URL"]
    _ensure_safe_test_target(db_url)
    engine = create_engine(db_url, pool_pre_ping=True)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with engine.connect() as conn:
        # 1. Verify partial indexes created by Alembic exist in Postgres catalogs
        idx_names = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = 'public' AND tablename = 'assessment_attempts'"
                )
            )
        }
        for required in (
            "uq_one_live_attempt",
            "ix_assessment_attempts_flagged",
            "ix_assessment_attempts_inflight_expiry",
        ):
            assert required in idx_names, f"Missing index {required}: found {idx_names}"

    # 2. Seed minimal fixtures and verify real lock_timeout & NOWAIT -> SQLSTATE 55P03
    s1 = Session()
    s2 = Session()
    seeded_ids = {}
    try:
        now = datetime.now(timezone.utc)
        emp = Employee(name="PG Test", email="pg_test@example.com", status="active", employee_type="Full-time")
        asm = Assessment(
            title="PG Lock Test",
            test_type="mcq",
            status="published",
            duration_minutes=10,
            passing_score=50.0,
            max_attempts=1,
            test_config={"categories": []},
            proctoring_config={},
        )
        s1.add_all([emp, asm])
        s1.flush()

        enr = AssessmentEnrollment(
            assessment_id=asm.id,
            employee_id=emp.id,
            status="in_progress",
        )
        s1.add(enr)
        s1.flush()

        att = AssessmentAttempt(
            enrollment_id=enr.id,
            attempt_number=1,
            assessment_version=1,
            status="in_progress",
            question_snapshot={"test_type": "mcq", "questions": []},
            expires_at=now + timedelta(minutes=10),
            answers={},
            answers_seq=0,
        )
        s1.add(att)
        s1.commit()
        seeded_ids = {"att_id": att.id, "enr_id": enr.id, "asm_id": asm.id, "emp_id": emp.id}

        # Session 1 holds FOR UPDATE OF assessment_enrollments + assessment_attempts
        locked_enr = (
            s1.query(AssessmentEnrollment)
            .options(joinedload(AssessmentEnrollment.assignment), joinedload(AssessmentEnrollment.assessment))
            .filter(AssessmentEnrollment.id == enr.id)
            .with_for_update(of=AssessmentEnrollment)
            .first()
        )
        assert locked_enr is not None
        locked_att = (
            s1.query(AssessmentAttempt)
            .filter(AssessmentAttempt.id == att.id)
            .with_for_update(of=AssessmentAttempt)
            .first()
        )
        assert locked_att is not None

        # Session 2 hits SET LOCAL lock_timeout = '200ms' on outer-joined FOR UPDATE OF AssessmentEnrollment
        try:
            _set_lock_timeout(s2, timeout_ms=200)
            s2.query(AssessmentEnrollment).options(
                joinedload(AssessmentEnrollment.assignment),
                joinedload(AssessmentEnrollment.assessment),
            ).filter(AssessmentEnrollment.id == enr.id).with_for_update(of=AssessmentEnrollment).first()
            raise AssertionError("Expected SQLSTATE 55P03 lock_timeout on concurrent FOR UPDATE")
        except OperationalError as exc:
            assert _is_lock_contention_error(exc), f"Unexpected OperationalError: {exc}"
            s2.rollback()

        # Session 2 hits FOR UPDATE OF assessment_attempts NOWAIT -> SQLSTATE 55P03
        try:
            s2.query(AssessmentAttempt).filter(AssessmentAttempt.id == att.id).with_for_update(
                of=AssessmentAttempt, nowait=True
            ).first()
            raise AssertionError("Expected SQLSTATE 55P03 NOWAIT lock error")
        except OperationalError as exc:
            assert _is_lock_contention_error(exc), f"Unexpected OperationalError: {exc}"
            s2.rollback()

        print("PostgreSQL integration checks passed cleanly.")
    finally:
        s2.close()
        s1.rollback()
        s1.close()
        if seeded_ids:
            with engine.begin() as cleanup_conn:
                cleanup_conn.execute(text("DELETE FROM assessment_attempts WHERE id = :id"), {"id": seeded_ids["att_id"]})
                cleanup_conn.execute(text("DELETE FROM assessment_enrollments WHERE id = :id"), {"id": seeded_ids["enr_id"]})
                cleanup_conn.execute(text("DELETE FROM assessments WHERE id = :id"), {"id": seeded_ids["asm_id"]})
                cleanup_conn.execute(text("DELETE FROM employees WHERE id = :id"), {"id": seeded_ids["emp_id"]})
        engine.dispose()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if mode == "bootstrap":
        bootstrap()
    else:
        verify()
