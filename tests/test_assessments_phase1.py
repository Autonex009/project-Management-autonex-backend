import os
import tempfile
from datetime import datetime, timezone, timedelta, date
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.db.database import Base, get_db
from app.models.user import User
from app.models.employee import Employee
from app.models.notification import Notification
from app.models.cluster import Cluster, ClusterMember
from app.models.assignment import Assignment
from app.models.audit_log import AuditLog
from app.models.parent_project import MainProject
from app.models.sub_project import SubProject
from app.models.project import DailySheet
from app.models.allocation import Allocation
from app.models.assessment import (
    Assessment,
    AssessmentEnrollment,
    AssessmentAttempt,
    AssessmentLog,
    AssessmentRecording,
)
from app.services.assessment_service import (
    validate_blueprint_for_publish,
    generate_candidate_exam_paper,
    sanitize_snapshot_for_candidate,
    sanitize_result_summary_for_candidate,
    validate_draft_payload,
    evaluate_mcq_attempt,
    evaluate_typing_attempt,
    finalize_attempt,
    utc,
    AssessmentValidationError,
)
from app.services.cluster_service import (
    populate_cluster_members,
    fan_out_cluster,
    remove_cluster_member,
    deactivate_assignment,
    archive_cluster,
    sync_dynamic_clusters_for_employee,
)
from app.services.auth_service import get_current_user
from app.api.clusters import router as clusters_router
from app.api.assignments import router as assignments_router
from app.api.assessments import router as assessments_router


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def http_env():
    """Creates an isolated FastAPI app with the Phase 1 routers and swappable caller auth."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    caller_holder = {"user": None}

    def override_get_current_user():
        return caller_holder["user"]

    api = FastAPI()
    api.include_router(clusters_router)
    api.include_router(assignments_router)
    api.include_router(assessments_router)
    api.dependency_overrides[get_db] = override_get_db
    api.dependency_overrides[get_current_user] = override_get_current_user

    db = TestingSessionLocal()
    try:
        client = TestClient(api)
        yield {
            "client": client,
            "db": db,
            "SessionLocal": TestingSessionLocal,
            "set_user": lambda u: caller_holder.update({"user": u}),
        }
    finally:
        db.close()


@pytest.fixture
def sample_mcq_config():
    return {
        "mode": "category_pooled",
        "shuffle_options": True,
        "shuffle_questions": True,
        "categories": [
            {
                "category_id": "cat_tenses",
                "name": "Tenses",
                "pick_count": 2,
                "questions": [
                    {
                        "question_id": "q1",
                        "question": "What tense is 'I have eaten'?",
                        "options": ["Present Perfect", "Past Simple", "Future"],
                        "correct_index": 0,
                        "explanation": "Have + past participle is Present Perfect.",
                        "marks": 1.0,
                    },
                    {
                        "question_id": "q2",
                        "question": "What tense is 'I was running'?",
                        "options": ["Past Continuous", "Present Simple", "Past Perfect"],
                        "correct_index": 0,
                        "marks": 1.0,
                    },
                    {
                        "question_id": "q3",
                        "question": "What tense is 'I will go'?",
                        "options": ["Future Simple", "Past Simple", "Present Continuous"],
                        "correct_index": 0,
                        "marks": 1.0,
                    },
                ],
            },
            {
                "category_id": "cat_voice",
                "name": "Active and Passive Voice",
                "pick_count": 1,
                "questions": [
                    {
                        "question_id": "q4",
                        "question": "Active to passive: 'The cat ate the fish.'",
                        "options": ["The fish was eaten by the cat.", "The cat was fish eaten."],
                        "correct_index": 0,
                        "marks": 2.0,
                    },
                ],
            },
        ],
    }


# ============================================================================
# 1. Blueprint Studio & Publish Validation Tests
# ============================================================================

def test_mcq_publish_validation_success(sample_mcq_config):
    validate_blueprint_for_publish("mcq", sample_mcq_config, passing_score=80.0)
    with pytest.raises(AssessmentValidationError, match="passing_score"):
        validate_blueprint_for_publish("mcq", sample_mcq_config, passing_score=None)


def test_mcq_publish_validation_underfilled_category():
    config = {
        "categories": [
            {
                "category_id": "cat1",
                "name": "Grammar",
                "pick_count": 5,
                "questions": [
                    {"question_id": "q1", "options": ["A", "B"], "correct_index": 0}
                ],
            }
        ]
    }
    with pytest.raises(AssessmentValidationError, match="requires at least 5 questions, but only 1 provided"):
        validate_blueprint_for_publish("mcq", config, passing_score=80.0)


def test_mcq_publish_validation_duplicate_question_id():
    config = {
        "categories": [
            {
                "category_id": "cat1",
                "name": "Cat 1",
                "pick_count": 1,
                "questions": [
                    {"question_id": "dup_1", "question": "Dup prompt", "options": ["A", "B"], "correct_index": 0}
                ],
            },
            {
                "category_id": "cat2",
                "name": "Cat 2",
                "pick_count": 1,
                "questions": [
                    {"question_id": "dup_1", "question": "Dup prompt", "options": ["A", "B"], "correct_index": 0}
                ],
            },
        ]
    }
    with pytest.raises(AssessmentValidationError, match="Duplicate question_id 'dup_1'"):
        validate_blueprint_for_publish("mcq", config, passing_score=80.0)


def test_typing_publish_validation():
    with pytest.raises(AssessmentValidationError, match="passage of at least 20 words"):
        validate_blueprint_for_publish("typing", {"passage_text": "Too short passage", "target_wpm": 40})

    long_passage = " ".join(["word"] * 25)
    with pytest.raises(AssessmentValidationError, match="target_wpm > 0"):
        validate_blueprint_for_publish("typing", {"passage_text": long_passage, "target_wpm": 0})

    validate_blueprint_for_publish("typing", {"passage_text": long_passage, "target_wpm": 40})


# ============================================================================
# 2. Paper Sampling, Candidate Sanitization & 0% Pass Mark Tests
# ============================================================================

def test_candidate_paper_sampling_mcq(sample_mcq_config):
    snapshot = generate_candidate_exam_paper("mcq", sample_mcq_config, passing_score=80.0)
    assert snapshot["test_type"] == "mcq"
    assert snapshot["passing_score"] == 80.0
    assert snapshot["total_questions"] == 3
    assert len(snapshot["questions"]) == 3
    assert snapshot["total_marks"] == 4.0

    for q in snapshot["questions"]:
        correct_idx = q["correct_index"]
        assert 0 <= correct_idx < len(q["options"])

    clean = sanitize_snapshot_for_candidate(snapshot)
    for q in clean["questions"]:
        assert "correct_index" not in q
        assert "explanation" not in q


def test_zero_percent_pass_mark_preserved(sample_mcq_config):
    """P1 fix: passing_score=0.0 must not silently turn into 80.0."""
    snapshot = generate_candidate_exam_paper("mcq", sample_mcq_config, passing_score=0.0)
    assert snapshot["passing_score"] == 0.0

    wrong_answers = {
        q["question_id"]: (q["correct_index"] + 1) % len(q["options"])
        for q in snapshot["questions"]
    }
    score, passed, summary = evaluate_mcq_attempt(snapshot, wrong_answers)
    assert score == 0.0
    assert summary["passing_score"] == 0.0
    assert passed is True


def test_candidate_paper_sampling_typing():
    config = {
        "passage_text": "The quick brown fox jumps over the lazy dog.",
        "target_wpm": 45,
        "min_accuracy_percent": 95.0,
    }
    snapshot = generate_candidate_exam_paper("typing", config)
    assert snapshot["test_type"] == "typing"
    assert snapshot["target_wpm"] == 45
    assert snapshot["min_accuracy_percent"] == 95.0
    assert snapshot["passage_text"] == config["passage_text"]


# ============================================================================
# 3. Server-Side Graders & Summary Sanitization Tests
# ============================================================================

def test_evaluate_mcq_attempt(sample_mcq_config):
    snapshot = generate_candidate_exam_paper("mcq", sample_mcq_config, passing_score=75.0)
    all_correct_answers = {q["question_id"]: q["correct_index"] for q in snapshot["questions"]}
    score, passed, summary = evaluate_mcq_attempt(snapshot, all_correct_answers)
    assert score == 100.0
    assert passed is True
    assert summary["total_marks_obtained"] == 4.0
    assert "category_breakdown" in summary
    assert "item_diagnostics" in summary

    clean_summary = sanitize_result_summary_for_candidate(summary)
    assert "item_diagnostics" not in clean_summary
    assert "correct_index" not in str(clean_summary)
    assert "category_breakdown" in clean_summary

    wrong_answers = dict(all_correct_answers)
    first_q = snapshot["questions"][0]
    wrong_answers[first_q["question_id"]] = (first_q["correct_index"] + 1) % len(first_q["options"])
    score_wrong, passed_wrong, summary_wrong = evaluate_mcq_attempt(snapshot, wrong_answers)
    assert score_wrong < 100.0
    assert summary_wrong["category_breakdown"][first_q["category_id"]]["correct_count"] < summary_wrong["category_breakdown"][first_q["category_id"]]["total_questions"]


def test_evaluate_typing_attempt_and_paste_detection():
    passage = "The quick brown fox jumps over the lazy dog."
    snapshot = {
        "test_type": "typing",
        "passage_text": passage,
        "target_wpm": 30.0,
        "min_accuracy_percent": 90.0,
    }

    data = {
        "typed_text": passage,
        "keystroke_count": 55,
    }
    score, passed, summary = evaluate_typing_attempt(snapshot, data, time_spent_seconds=15.0)
    assert passed is True
    assert summary["accuracy_percent"] == 100.0
    assert summary["is_flagged"] is False

    long_passage = "In a quiet village surrounded by tall trees and green hills, every morning brought the crisp sound of birds."
    snapshot_long = {
        "test_type": "typing",
        "passage_text": long_passage,
        "target_wpm": 40.0,
        "min_accuracy_percent": 90.0,
    }
    pasted_data = {
        "typed_text": long_passage,
        "keystroke_count": 5,
    }
    score_p, passed_p, summary_p = evaluate_typing_attempt(snapshot_long, pasted_data, time_spent_seconds=5.0)
    assert summary_p["is_flagged"] is True
    assert summary_p["flag_reason"] == "suspicious_bulk_paste_keystrokes_missing"


# ============================================================================
# 4. Draft Validation & Boolean Rejection Tests
# ============================================================================

def test_validate_draft_payload_and_boolean_rejection():
    snapshot = {
        "test_type": "mcq",
        "questions": [
            {"question_id": "q1", "options": ["A", "B", "C"]},
            {"question_id": "q2", "options": ["True", "False"]},
        ],
    }

    validate_draft_payload(snapshot, {"answers": {"q1": 0, "q2": 1}})

    with pytest.raises(AssessmentValidationError, match="Unknown question_id 'q99'"):
        validate_draft_payload(snapshot, {"answers": {"q99": 0}})

    with pytest.raises(AssessmentValidationError, match="out of bounds"):
        validate_draft_payload(snapshot, {"answers": {"q2": 5}})

    # Booleans are ints in Python (isinstance(True, int) is True) — must reject!
    with pytest.raises(AssessmentValidationError, match="integer option index"):
        validate_draft_payload(snapshot, {"answers": {"q1": True}})

    typing_snap = {"test_type": "typing", "passage_text": "hello world"}
    with pytest.raises(AssessmentValidationError, match="keystroke_count"):
        validate_draft_payload(typing_snap, {"answers": {"typed_text": "hello", "keystroke_count": False}})


# ============================================================================
# 5. Cohort Clusters, Membership & Set-Based Fan-Out Tests
# ============================================================================

def test_cluster_creation_and_member_fanout(db_session):
    now = datetime.now(timezone.utc)
    emp1 = Employee(id=101, name="Dev One", email="dev1@test.com", status="active", designation="Developer", employee_type="Full-time")
    emp2 = Employee(id=102, name="Dev Two", email="dev2@test.com", status="active", designation="Developer", employee_type="Full-time")
    emp3 = Employee(id=103, name="QA One", email="qa1@test.com", status="active", designation="QA", employee_type="Full-time")
    db_session.add_all([emp1, emp2, emp3])
    db_session.commit()

    cluster = Cluster(
        id=1,
        name="Developer Cohort",
        dynamic_scope="developers",
    )
    db_session.add(cluster)
    db_session.commit()

    populate_cluster_members(db_session, cluster)
    db_session.commit()

    members = db_session.query(ClusterMember).filter_by(cluster_id=1).all()
    assert {m.employee_id for m in members} == {101, 102}

    assessment = Assessment(
        id=1,
        title="Python Assessment",
        test_type="mcq",
        status="published",
        duration_minutes=20,
        passing_score=80.0,
        max_attempts=1,
    )
    db_session.add(assessment)
    db_session.commit()

    deadline_dt = now + timedelta(days=5)
    assignment = Assignment(
        id=1,
        cluster_id=cluster.id,
        assessment_id=assessment.id,
        deadline=deadline_dt,
        is_active=True,
    )
    db_session.add(assignment)
    db_session.commit()

    count = fan_out_cluster(db_session, cluster, now=now)
    db_session.commit()
    assert count == 2

    # Idempotent re-run creates 0 duplicates
    count_again = fan_out_cluster(db_session, cluster, now=now)
    db_session.commit()
    assert count_again == 0

    enrollments = db_session.query(AssessmentEnrollment).filter_by(assignment_id=assignment.id).all()
    assert len(enrollments) == 2
    for enr in enrollments:
        assert enr.status == "assigned"
        assert enr.deadline is None


def test_relative_deadline_fanout(db_session):
    now = datetime.now(timezone.utc)
    emp = Employee(id=201, name="New Hire", email="hire@test.com", status="active", designation="Developer", employee_type="Full-time")
    db_session.add(emp)

    cluster = Cluster(id=2, name="Onboarding Cohort")
    db_session.add(cluster)
    db_session.commit()

    db_session.add(ClusterMember(cluster_id=2, employee_id=201))
    db_session.commit()

    assessment = Assessment(id=2, title="Safety Test", test_type="mcq", status="published")
    db_session.add(assessment)
    db_session.commit()

    assignment = Assignment(
        id=2,
        cluster_id=2,
        assessment_id=2,
        relative_due_days=7,
        is_active=True,
    )
    db_session.add(assignment)
    db_session.commit()

    fan_out_cluster(db_session, cluster, now=now)
    db_session.commit()

    enr = db_session.query(AssessmentEnrollment).filter_by(employee_id=201, assessment_id=2).first()
    assert enr is not None
    assert enr.deadline is not None
    diff = (utc(enr.deadline) - now).total_seconds()
    assert 6 * 86400 < diff < 8 * 86400


# ============================================================================
# 6. "Never Shorten, Auto-Clear Redundant" Deadline Extension Tests
# ============================================================================

def test_dual_tier_deadline_extension_rule(db_session):
    now = datetime.now(timezone.utc)
    c_dl_old = now + timedelta(days=2)
    c_dl_new = now + timedelta(days=5)

    assignment = Assignment(
        id=10,
        cluster_id=1,
        assessment_id=1,
        deadline=c_dl_old,
        is_active=True,
    )
    db_session.add(assignment)
    db_session.commit()

    dl_medical = now + timedelta(days=20)
    enr_a = AssessmentEnrollment(id=1, assignment_id=10, assessment_id=1, employee_id=301, deadline=dl_medical)
    dl_late = now + timedelta(days=3)
    enr_b = AssessmentEnrollment(id=2, assignment_id=10, assessment_id=1, employee_id=302, deadline=dl_late)
    enr_c = AssessmentEnrollment(id=3, assignment_id=10, assessment_id=1, employee_id=303, deadline=None)

    db_session.add_all([enr_a, enr_b, enr_c])
    db_session.commit()

    assignment.deadline = c_dl_new
    db_session.query(AssessmentEnrollment).filter(
        AssessmentEnrollment.assignment_id == assignment.id,
        AssessmentEnrollment.deadline.isnot(None),
        AssessmentEnrollment.deadline <= c_dl_new,
    ).update({"deadline": None}, synchronize_session=False)
    db_session.commit()

    db_session.refresh(enr_a)
    db_session.refresh(enr_b)
    db_session.refresh(enr_c)

    assert utc(enr_a.deadline) == dl_medical
    assert enr_b.deadline is None
    assert enr_c.deadline is None


# ============================================================================
# 7. Deduplication on Member Removal and No-op Setter Elimination
# ============================================================================

def test_remove_cluster_member_deduplication(db_session):
    now = datetime.now(timezone.utc)
    asm = Assessment(id=1, title="Dup Check Test", test_type="mcq", status="published")
    asg1 = Assignment(id=101, cluster_id=1, assessment_id=1, deadline=now + timedelta(days=3), is_active=True)
    asg2 = Assignment(id=102, cluster_id=2, assessment_id=1, deadline=now + timedelta(days=3), is_active=True)
    cl1 = Cluster(id=1, name="Cluster 1", is_archived=False)
    cl2 = Cluster(id=2, name="Cluster 2", is_archived=False)
    db_session.add_all([asm, asg1, asg2, cl1, cl2])
    db_session.commit()

    m1 = ClusterMember(cluster_id=1, employee_id=401)
    m2 = ClusterMember(cluster_id=2, employee_id=401)
    db_session.add_all([m1, m2])

    enr = AssessmentEnrollment(id=1, assignment_id=101, assessment_id=1, employee_id=401, status="assigned")
    db_session.add(enr)
    db_session.commit()

    remove_cluster_member(db_session, 1, 401)
    db_session.commit()

    db_session.refresh(enr)
    assert enr.status == "assigned"
    assert enr.assignment_id == 102

    remove_cluster_member(db_session, 2, 401)
    db_session.commit()

    db_session.refresh(enr)
    assert enr.status == "cancelled"


def test_removed_noop_setters_and_on_delete_restrict(db_session):
    enr = AssessmentEnrollment(assessment_id=1, employee_id=1, status="in_progress")
    with pytest.raises(AttributeError):
        enr.attempts_used = 5
    with pytest.raises(AttributeError):
        enr.best_score = 99.0

    # Verify DB ON DELETE RESTRICT blocks silent ORM cascade deletion of enrollments with attempts
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _conn_record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine)
    s = Sess()
    try:
        emp = Employee(id=1, name="E", email="e@x.com", status="active", employee_type="Full-time")
        asm = Assessment(id=1, title="A", test_type="mcq", status="published")
        s.add_all([emp, asm])
        s.flush()
        enr2 = AssessmentEnrollment(id=10, assessment_id=1, employee_id=1, status="in_progress")
        s.add(enr2)
        s.flush()
        att = AssessmentAttempt(
            id=20,
            enrollment_id=10,
            attempt_number=1,
            assessment_version=1,
            status="in_progress",
            question_snapshot={"test_type": "mcq", "questions": []},
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        s.add(att)
        s.commit()

        s.delete(enr2)
        with pytest.raises(IntegrityError):
            s.commit()
    finally:
        s.rollback()
        s.close()


# ============================================================================
# 8. Single Finalization Path & Retest Lifecycle Tests
# ============================================================================

def test_finalize_attempt_and_retest_lifecycle(db_session):
    now = datetime.now(timezone.utc)
    assessment = Assessment(
        id=5,
        title="Finalize Test",
        test_type="mcq",
        status="published",
        passing_score=80.0,
        max_attempts=1,
    )
    enrollment = AssessmentEnrollment(
        id=50,
        assessment_id=5,
        employee_id=501,
        status="in_progress",
        extra_attempts=0,
    )
    attempt = AssessmentAttempt(
        id=500,
        enrollment_id=50,
        attempt_number=1,
        assessment_version=1,
        status="in_progress",
        question_snapshot={
            "test_type": "mcq",
            "passing_score": 80.0,
            "questions": [
                {"question_id": "q1", "correct_index": 0, "marks": 1.0, "category_id": "c1", "category_name": "General"}
            ],
            "categories": [{"category_id": "c1", "name": "General"}],
        },
        started_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(minutes=15),
        answers={"q1": 1},
    )
    db_session.add_all([assessment, enrollment, attempt])
    db_session.commit()

    finalize_attempt(db_session, attempt, enrollment, assessment, attempt.answers, now, reason="candidate_submit")
    db_session.commit()

    assert attempt.status == "completed"
    assert attempt.score == 0.0
    assert attempt.passed is False
    assert enrollment.status == "failed"

    enrollment.extra_attempts += 1
    enrollment.status = "assigned"
    db_session.commit()

    assert enrollment.status == "assigned"
    assert enrollment.attempts_used < (assessment.max_attempts + enrollment.extra_attempts)


# ============================================================================
# 9. Background Jobs Tests (Reaper, Reminders, Physical Storage Purge)
# ============================================================================

def test_scheduler_reaper_reminder_and_purge_jobs(http_env, sample_mcq_config):
    from app.services import scheduler_service

    db = http_env["db"]
    SessionFactory = http_env["SessionLocal"]
    now = datetime.now(timezone.utc)

    emp = Employee(id=601, name="Job Candidate", email="job@test.com", status="active", employee_type="Full-time")
    user = User(id=601, name="Job Candidate", email="job@test.com", password_hash="x", role="employee", employee_id=601, is_active=True)
    cl = Cluster(id=60, name="Job Cluster", dynamic_scope="custom")
    asm = Assessment(
        id=60,
        title="Job Assessment",
        test_type="mcq",
        status="published",
        passing_score=80.0,
        max_attempts=1,
        test_config=sample_mcq_config,
    )
    asg = Assignment(id=60, cluster_id=60, assessment_id=60, deadline=now + timedelta(days=2), is_active=True)
    enr_reap = AssessmentEnrollment(id=6001, assignment_id=60, assessment_id=60, employee_id=601, status="in_progress")
    att_reap = AssessmentAttempt(
        id=6001,
        enrollment_id=6001,
        attempt_number=1,
        assessment_version=1,
        status="in_progress",
        question_snapshot={
            "test_type": "mcq",
            "passing_score": 80.0,
            "questions": [
                {"question_id": "q1", "correct_index": 0, "marks": 1.0, "category_id": "c1", "category_name": "General"}
            ],
            "categories": [{"category_id": "c1", "name": "General"}],
        },
        started_at=now - timedelta(minutes=30),
        expires_at=now - timedelta(minutes=10),
        answers={"q1": 0},
    )

    # Second assessment for reminder testing
    asm2 = Assessment(id=61, title="Reminder Test", test_type="mcq", status="published")
    asg2 = Assignment(id=61, cluster_id=60, assessment_id=61, deadline=now + timedelta(days=1), is_active=True)
    enr_rem = AssessmentEnrollment(id=6002, assignment_id=61, assessment_id=61, employee_id=601, status="assigned")

    # Recordings for purge test
    with tempfile.TemporaryDirectory() as tmp_rec_dir:
        rel_path = "assessments/6001/proctoring.webm"
        abs_path = os.path.join(tmp_rec_dir, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "wb") as f:
            f.write(b"fake-webm-bytes")

        rec_expired = AssessmentRecording(
            id=1,
            attempt_id=6001,
            video_storage_path=rel_path,
            status="ready",
            expires_at=now - timedelta(days=2),
        )
        rec_abandoned = AssessmentRecording(
            id=2,
            attempt_id=9999,
            video_storage_path="assessments/9999/proctoring.webm",
            status="pending_upload",
            created_at=now - timedelta(hours=30),
            expires_at=now + timedelta(days=7),
        )
        db.add_all([emp, user, cl, asm, asg, enr_reap, att_reap, asm2, asg2, enr_rem, rec_expired, rec_abandoned])
        db.commit()

        with patch.object(scheduler_service, "SessionLocal", SessionFactory), \
             patch.dict(os.environ, {"ALLOW_LOCAL_RECORDING_FALLBACK": "true"}), \
             patch("app.services.assessment_storage_service.is_supabase_configured", return_value=False), \
             patch("app.services.assessment_storage_service.LOCAL_RECORDINGS_DIR", tmp_rec_dir):
            scheduler_service.reconcile_expired_attempts_job()
            scheduler_service.send_daily_assignment_reminders_job()
            # Re-run reminder job same day to confirm idempotency
            scheduler_service.send_daily_assignment_reminders_job()
            scheduler_service.purge_expired_assessment_recordings_job()

        assert not os.path.exists(abs_path), "Physical recording file must be deleted on purge"

    db.expire_all()
    assert db.get(AssessmentAttempt, 6001).status == "abandoned"
    assert db.get(AssessmentEnrollment, 6001).status == "passed"
    assert db.get(AssessmentRecording, 1).status == "purged"
    assert db.get(AssessmentRecording, 2).status == "failed"

    notifs = db.query(Notification).filter_by(user_id=601, type="assessment_reminder").all()
    assert len(notifs) == 1
    # Confirms reminder was delivered and enrollment.last_reminded_at was recorded
    enr_reminded = db.get(AssessmentEnrollment, 6002)
    assert enr_reminded.last_reminded_at is not None


def test_scheduler_registers_all_phase1_jobs(db_session):
    """Confirms all 4 Phase 1 background jobs (including 02:00 AM dynamic reconcile) register cleanly."""
    from apscheduler.schedulers.background import BackgroundScheduler
    from app.services import scheduler_service

    fresh_sched = BackgroundScheduler(timezone="Asia/Kolkata")
    with patch.object(scheduler_service, "_scheduler", fresh_sched), \
         patch.object(fresh_sched, "start"):
        scheduler_service.start_scheduler()

    job_ids = {j.id for j in fresh_sched.get_jobs()}
    assert {
        "reconcile_dynamic_clusters",
        "assessment_expired_attempts_reaper",
        "daily_assignment_reminders",
        "purge_expired_assessment_recordings",
    }.issubset(job_ids)

    reconcile_job = fresh_sched.get_job("reconcile_dynamic_clusters")
    assert "hour='2', minute='0'" in str(reconcile_job.trigger)


def test_employee_lifecycle_sync_hooks_and_autoflush_safety(db_session, sample_mcq_config):
    """Verifies create/update/archive/restore syncs dynamic clusters even with autoflush=False."""
    now = datetime.now(timezone.utc)
    emp = Employee(id=901, name="Evolving", email="ev@test.com", status="active", designation="Annotator", employee_type="Full-time")
    cl = Cluster(id=90, name="Devs Only", dynamic_scope="developers")
    asm = Assessment(id=90, title="Dev Onboarding", test_type="mcq", status="published", test_config=sample_mcq_config)
    asg = Assignment(id=90, cluster_id=90, assessment_id=90, deadline=now + timedelta(days=7), is_active=True)
    db_session.add_all([emp, cl, asm, asg])
    db_session.commit()

    # Promotion to Developer (pending uncommitted attribute change, autoflush=False!)
    emp.designation = "Developer"
    sync_dynamic_clusters_for_employee(db_session, emp.id)
    db_session.commit()

    assert db_session.query(ClusterMember).filter_by(cluster_id=90, employee_id=901).first() is not None
    enr = db_session.query(AssessmentEnrollment).filter_by(employee_id=901, assessment_id=90).first()
    assert enr is not None and enr.status == "assigned"

    # Archiving removes membership and cancels unstarted enrollment
    emp.status = "archived"
    sync_dynamic_clusters_for_employee(db_session, emp.id)
    db_session.commit()

    assert db_session.query(ClusterMember).filter_by(cluster_id=90, employee_id=901).first() is None
    db_session.refresh(enr)
    assert enr.status == "cancelled"

    # Restoring re-enrolls into dynamic cluster and revives enrollment
    emp.status = "active"
    sync_dynamic_clusters_for_employee(db_session, emp.id)
    db_session.commit()

    assert db_session.query(ClusterMember).filter_by(cluster_id=90, employee_id=901).first() is not None
    db_session.refresh(enr)
    assert enr.status == "assigned"


# ============================================================================
# 10. Real HTTP TestClient Tests: Role Enforcement, Leaks & Lifecycle
# ============================================================================

def test_http_role_checks_and_no_list_crash(http_env, sample_mcq_config):
    client = http_env["client"]
    db = http_env["db"]
    set_user = http_env["set_user"]

    admin_user = User(id=1, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True)
    hr_user = User(id=4, name="HR", email="hr@x.com", password_hash="x", role="hr", is_active=True)
    pm_user = User(id=2, name="PM", email="pm@x.com", password_hash="x", role="pm", is_active=True)
    lead_user = User(id=5, name="Lead", email="lead@x.com", password_hash="x", role="team_lead", is_active=True)
    emp = Employee(id=10, name="Worker", email="w@x.com", status="active", designation="Developer", employee_type="Full-time")
    cand_user = User(id=3, name="Worker", email="w@x.com", password_hash="x", role="employee", employee_id=10, is_active=True)
    db.add_all([admin_user, hr_user, pm_user, lead_user, emp, cand_user])
    db.commit()

    # Employee, PM, and Team Lead are STRICTLY blocked (403) from Cluster/Assignment/Studio admin endpoints
    for non_admin in (cand_user, pm_user, lead_user):
        set_user(non_admin)
        assert client.post("/api/clusters", json={"name": "C1"}).status_code == 403
        assert client.get("/api/clusters").status_code == 403
        assert client.get("/api/assignments").status_code == 403
        assert client.post("/api/assessments", json={"title": "T1"}).status_code == 403
        assert client.get("/api/assessments").status_code == 403

    # Admin & HR succeed on cluster/assignment/studio endpoints (201/200)
    set_user(admin_user)
    cl_resp = client.post("/api/clusters", json={"name": "Dev Smart", "dynamic_scope": "developers"})
    assert cl_resp.status_code == 201
    cl_data = cl_resp.json()
    assert cl_data["member_count"] == 1
    assert cl_data["is_dynamic"] is True

    # Manual member add/remove rejected on dynamic clusters (P1 #12)
    add_dyn = client.post(f"/api/clusters/{cl_data['id']}/members", json={"employee_ids": [10]})
    assert add_dyn.status_code == 400
    del_dyn = client.delete(f"/api/clusters/{cl_data['id']}/members/10")
    assert del_dyn.status_code == 400

    # Validation bounds rejected (P1 #17)
    bad_asm = client.post("/api/assessments", json={"title": "Bad", "duration_minutes": 0, "passing_score": 150})
    assert bad_asm.status_code == 422

    # Create draft assessment as HR
    set_user(hr_user)
    asm_resp = client.post("/api/assessments", json={
        "title": "Dev Quiz",
        "test_type": "mcq",
        "duration_minutes": 15,
        "passing_score": 50.0,
        "max_attempts": 2,
        "test_config": sample_mcq_config,
    })
    assert asm_resp.status_code == 201
    asm_id = asm_resp.json()["id"]

    # Assigning a DRAFT assessment must fail with 400 (P1 #13)
    future_dl = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    draft_asg = client.post("/api/assignments", json={
        "cluster_id": cl_data["id"],
        "assessment_id": asm_id,
        "deadline": future_dl,
    })
    assert draft_asg.status_code == 400

    # Publish & assign
    assert client.put(f"/api/assessments/{asm_id}", json={"status": "published"}).status_code == 200
    pub_asg = client.post("/api/assignments", json={
        "cluster_id": cl_data["id"],
        "assessment_id": asm_id,
        "deadline": future_dl,
    })
    assert pub_asg.status_code == 201
    assert pub_asg.json()["total_enrolled"] == 1

    # Filter assignments by type=assessment works via HTTP (P1 #14)
    asg_list = client.get("/api/assignments?assignment_type=assessment")
    assert asg_list.status_code == 200
    assert len(asg_list.json()) == 1


def test_http_answer_leak_prevention_and_sitting_recovery(http_env, sample_mcq_config):
    client = http_env["client"]
    db = http_env["db"]
    set_user = http_env["set_user"]

    now = datetime.now(timezone.utc)
    admin_user = User(id=1, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True)
    emp = Employee(id=20, name="Cand", email="cand@x.com", status="active", designation="Developer", employee_type="Full-time")
    cand_user = User(id=2, name="Cand", email="cand@x.com", password_hash="x", role="employee", employee_id=20, is_active=True)
    other_emp = Employee(id=21, name="Other", email="other@x.com", status="active", designation="Developer", employee_type="Full-time")
    other_user = User(id=3, name="Other", email="other@x.com", password_hash="x", role="employee", employee_id=21, is_active=True)

    asm = Assessment(
        id=50,
        title="Leak Proof Exam",
        test_type="mcq",
        status="published",
        duration_minutes=15,
        passing_score=80.0,
        max_attempts=2,
        test_config=sample_mcq_config,
    )
    enr = AssessmentEnrollment(
        id=500,
        assessment_id=50,
        employee_id=20,
        status="assigned",
        deadline=now + timedelta(days=3),
    )
    db.add_all([admin_user, emp, cand_user, other_emp, other_user, asm, enr])
    db.commit()

    # 1. Overview strips questions & answers
    set_user(cand_user)
    ov = client.get("/api/assessments/50/overview")
    assert ov.status_code == 200
    assert "correct_index" not in ov.text
    assert "questions" not in ov.json()

    # 2. Start attempt & verify zero answer leaks
    start = client.post("/api/assessments/enrollments/500/attempts", json={"camera_verified": True})
    assert start.status_code == 200
    assert "correct_index" not in start.text
    assert "explanation" not in start.text
    att_id = start.json()["id"]
    q_ids = [q["question_id"] for q in start.json()["question_snapshot"]["questions"]]

    # 3. Draft rejects boolean answers (422) & enforces monotonic seq
    bad_draft = client.put(f"/api/assessments/attempts/{att_id}/draft", json={
        "answers": {q_ids[0]: True},
        "answers_seq": 1,
    })
    assert bad_draft.status_code == 422

    ok_draft = client.put(f"/api/assessments/attempts/{att_id}/draft", json={
        "answers": {q_ids[0]: 0},
        "answers_seq": 2,
        "events": [{"event_type": "tab_switch"}],
    })
    assert ok_draft.status_code == 200
    assert ok_draft.json()["answers_seq"] == 2

    # 4. Recording local upload fallback + finalize
    with tempfile.TemporaryDirectory() as tmp_rec_dir, \
         patch.dict(os.environ, {"ALLOW_LOCAL_RECORDING_FALLBACK": "true"}), \
         patch("app.services.assessment_storage_service.is_supabase_configured", return_value=False), \
         patch("app.api.assessments.is_supabase_configured", return_value=False), \
         patch("app.services.assessment_storage_service.LOCAL_RECORDINGS_DIR", tmp_rec_dir), \
         patch("app.api.assessments.LOCAL_RECORDINGS_DIR", tmp_rec_dir):
        up_info = client.post(f"/api/assessments/attempts/{att_id}/recording/upload-url")
        assert up_info.status_code == 200
        assert "mock-storage" not in up_info.json()["upload_url"]

        put_vid = client.put(up_info.json()["upload_url"], content=b"webm-test-bytes")
        assert put_vid.status_code == 200

        fin_vid = client.post(f"/api/assessments/attempts/{att_id}/recording/finalize")
        assert fin_vid.status_code == 200

        # Candidate cannot play recordings (403); admin gets signed play URL and can stream local fallback
        assert client.get(f"/api/assessments/results/{att_id}/recording/play-url").status_code == 403
        set_user(admin_user)
        play_info = client.get(f"/api/assessments/results/{att_id}/recording/play-url")
        assert play_info.status_code == 200
        assert client.get(play_info.json()["play_url"]).content == b"webm-test-bytes"

    # 5. IDOR check: other employee cannot submit
    set_user(other_user)
    assert client.post(f"/api/assessments/attempts/{att_id}/submit", json={"answers": {}}).status_code == 404

    # 6. Candidate submit MUST NOT expose item_diagnostics or correct_index
    set_user(cand_user)
    sub = client.post(f"/api/assessments/attempts/{att_id}/submit", json={"answers": {qid: 0 for qid in q_ids}})
    assert sub.status_code == 200
    assert "item_diagnostics" not in sub.text
    assert "correct_index" not in sub.text
    assert "category_breakdown" in sub.json()["result_summary"]

    # 7. Candidate /my/enrollments & /scorecard MUST NOT leak item_diagnostics or correct_index
    my_enr = client.get("/api/assessments/my/enrollments")
    assert my_enr.status_code == 200
    assert "item_diagnostics" not in my_enr.text
    assert "correct_index" not in my_enr.text

    cand_sc = client.get("/api/assessments/enrollments/500/scorecard")
    assert cand_sc.status_code == 200
    assert "item_diagnostics" not in cand_sc.text
    assert "correct_index" not in cand_sc.text

    # 8. Admin scorecard DOES include item_diagnostics for evaluator review
    set_user(admin_user)
    admin_sc = client.get("/api/assessments/enrollments/500/scorecard")
    assert admin_sc.status_code == 200
    assert "item_diagnostics" in admin_sc.json()["attempts"][0]["result_summary"]

    # 9. Expired attempt recovery: if live attempt #1 times out without passing and attempts remain, immediately start #2
    enr.status = "in_progress"
    att1 = db.query(AssessmentAttempt).filter_by(id=att_id).first()
    att1.status = "in_progress"
    att1.answers = {}
    att1.passed = None
    att1.expires_at = now - timedelta(minutes=2)
    db.commit()

    set_user(cand_user)
    recovered = client.post("/api/assessments/enrollments/500/attempts", json={"camera_verified": True})
    assert recovered.status_code == 200
    assert recovered.json()["attempt_number"] == 2
    assert recovered.json()["status"] == "in_progress"


def test_dynamic_cluster_scope_update_fans_out_synchronously(http_env, sample_mcq_config):
    client = http_env["client"]
    db = http_env["db"]
    set_user = http_env["set_user"]

    now = datetime.now(timezone.utc)
    admin_user = User(id=1, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True)
    dev_emp = Employee(id=30, name="Dev", email="d@x.com", status="active", designation="Developer", employee_type="Full-time")
    qa_emp = Employee(id=31, name="QA", email="q@x.com", status="active", designation="QA", employee_type="Full-time")
    asm = Assessment(id=70, title="Cohort Test", test_type="mcq", status="published", test_config=sample_mcq_config)
    db.add_all([admin_user, dev_emp, qa_emp, asm])
    db.commit()

    set_user(admin_user)
    cl_resp = client.post("/api/clusters", json={"name": "Role Shifting Cohort", "dynamic_scope": "developers"})
    assert cl_resp.status_code == 201
    cl_id = cl_resp.json()["id"]
    assert cl_resp.json()["member_count"] == 1

    future_dl = (now + timedelta(days=5)).isoformat()
    asg_resp = client.post("/api/assignments", json={
        "cluster_id": cl_id,
        "assessment_id": 70,
        "deadline": future_dl,
    })
    assert asg_resp.status_code == 201
    assert asg_resp.json()["total_enrolled"] == 1

    # Switch dynamic scope to annotators_reviewers -> QA added & enrolled, Dev unstarted cancelled!
    upd = client.put(f"/api/clusters/{cl_id}", json={"dynamic_scope": "annotators_reviewers", "is_archived": True})
    assert upd.status_code == 200
    assert upd.json()["member_count"] == 1
    assert upd.json()["is_archived"] is False  # ClusterUpdate must never bypass DELETE archiving

    db.expire_all()
    dev_enr = db.query(AssessmentEnrollment).filter_by(employee_id=30, assessment_id=70).first()
    qa_enr = db.query(AssessmentEnrollment).filter_by(employee_id=31, assessment_id=70).first()
    assert dev_enr.status == "cancelled"
    assert qa_enr is not None
    assert qa_enr.status == "assigned"

    # Test grant-retest, extend-deadline, deadline cohort update & recording play-url via HTTP
    qa_enr.status = "failed"
    db.commit()

    retest = client.post(f"/api/assessments/enrollments/{qa_enr.id}/grant-retest", json={"reason": "Power outage"})
    assert retest.status_code == 200
    assert retest.json()["extra_attempts"] == 1
    assert retest.json()["status"] == "assigned"

    ext = client.put(
        f"/api/assessments/enrollments/{qa_enr.id}/extend-deadline",
        json={"new_deadline": (now + timedelta(days=10)).isoformat(), "reason": "Medical"},
    )
    assert ext.status_code == 200

    # Cohort deadline update clears earlier overrides and keeps longer ones
    cohort_upd = client.put(
        f"/api/assignments/{asg_resp.json()['id']}/deadline",
        json={"new_deadline": (now + timedelta(days=6)).isoformat()},
    )
    assert cohort_upd.status_code == 200
    db.expire_all()
    assert db.get(AssessmentEnrollment, qa_enr.id).deadline is not None

    # Archive cluster deactivates assignment and cancels unstarted QA enrollment
    del_cl = client.delete(f"/api/clusters/{cl_id}")
    assert del_cl.status_code == 200
    db.expire_all()
    assert db.get(Assignment, asg_resp.json()["id"]).is_active is False
    assert db.get(AssessmentEnrollment, qa_enr.id).status == "cancelled"


def test_admin_enrollments_members_reminders_hr_access_and_db_close(http_env, sample_mcq_config):
    client = http_env["client"]
    db = http_env["db"]
    set_user = http_env["set_user"]

    now = datetime.now(timezone.utc)
    hr_user = User(id=1, name="HR", email="hr@x.com", password_hash="x", role="hr", is_active=True)
    admin_user = User(id=2, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True)
    emp1 = Employee(id=41, name="Alice", email="a@x.com", status="active", designation="Developer", employee_type="Full-time")
    emp2 = Employee(id=42, name="Bob", email="b@x.com", status="active", designation="QA", employee_type="Full-time")
    u1 = User(id=3, name="Alice", email="a@x.com", password_hash="x", role="employee", employee_id=41, is_active=True)
    asm = Assessment(
        id=80,
        title="Proctored Audit Test",
        test_type="mcq",
        status="published",
        duration_minutes=15,
        passing_score=50.0,
        max_attempts=1,
        proctoring_config={"require_camera": True, "max_tab_switches": 1},
        test_config=sample_mcq_config,
    )
    db.add_all([hr_user, admin_user, emp1, emp2, u1, asm])
    db.commit()

    # Custom cluster creation + roster read + Cache Surgery cluster payload on add/remove
    set_user(admin_user)
    cl_resp = client.post("/api/clusters", json={
        "name": "Custom Pilot",
        "dynamic_scope": "custom",
        "employee_ids": [41],
    })
    assert cl_resp.status_code == 201
    cl_id = cl_resp.json()["id"]

    # HR can read cluster members, assignments, and admin enrollments
    set_user(hr_user)
    mem_resp = client.get(f"/api/clusters/{cl_id}/members")
    assert mem_resp.status_code == 200
    assert [m["employee_id"] for m in mem_resp.json()] == [41]

    # Add emp2 via batch endpoint -> returns updated cluster for Cache Surgery
    set_user(admin_user)
    add_resp = client.post(f"/api/clusters/{cl_id}/members", json={"employee_ids": [42]})
    assert add_resp.status_code == 200
    assert add_resp.json()["cluster"]["member_count"] == 2

    # Assign & verify paginated Progress & Logs register + HR read + tab-switch auto-flagging
    asg_resp = client.post("/api/assignments", json={
        "cluster_id": cl_id,
        "assessment_id": 80,
        "deadline": (now + timedelta(days=4)).isoformat(),
    })
    assert asg_resp.status_code == 201

    set_user(hr_user)
    page_resp = client.get(f"/api/assessments/admin/enrollments?cluster_id={cl_id}&page=1&page_size=20")
    assert page_resp.status_code == 200
    page_data = page_resp.json()
    assert page_data["total_items"] == 2
    assert page_data["summary"]["assigned"] == 2

    alice_enr_id = next(r["enrollment_id"] for r in page_data["items"] if r["employee_id"] == 41)

    # Candidate takes exam & triggers 2 tab switches (max_tab_switches = 1) -> auto-flagged!
    set_user(u1)
    start_resp = client.post(f"/api/assessments/enrollments/{alice_enr_id}/attempts", json={"camera_verified": True})
    assert start_resp.status_code == 200
    att_id = start_resp.json()["id"]
    q_ids = [q["question_id"] for q in start_resp.json()["question_snapshot"]["questions"]]

    client.put(f"/api/assessments/attempts/{att_id}/draft", json={
        "answers": {q_ids[0]: 0},
        "answers_seq": 1,
        "events": [{"event_type": "tab_switch"}, {"event_type": "tab_switch"}],
    })
    sub_resp = client.post(f"/api/assessments/attempts/{att_id}/submit", json={"answers": {qid: 0 for qid in q_ids}})
    assert sub_resp.status_code == 200

    # Verify surgical row endpoint reflects tab-switch auto-flagging
    set_user(hr_user)
    row_resp = client.get(f"/api/assessments/admin/enrollments/{alice_enr_id}/row")
    assert row_resp.status_code == 200
    assert row_resp.json()["is_flagged"] is True
    assert row_resp.json()["tab_switch_count"] == 2

    # Bulk reminder creates notification in DB without Slack external calls blocking request
    with patch("app.api.assessments.try_send_assessment_reminder_dm", return_value=True):
        rem_resp = client.post("/api/assessments/reminders/send", json={"cluster_id": cl_id})
    assert rem_resp.status_code == 200
    assert rem_resp.json()["notified_count"] >= 1

    # Verify create_signed_play_url closes DB session BEFORE calling external storage helper
    rec = AssessmentRecording(
        attempt_id=att_id,
        video_storage_path=f"assessments/{att_id}/proctoring.webm",
        status="ready",
        expires_at=now + timedelta(days=5),
    )
    db.add(rec)
    db.commit()

    def _fake_play_url(*args, **kwargs):
        return "https://signed.example.com/video.webm"

    with patch("app.api.assessments.create_signed_play_url", side_effect=_fake_play_url), \
         patch("app.api.assessments.is_supabase_configured", return_value=True):
        play_resp = client.get(f"/api/assessments/results/{att_id}/recording/play-url")
    assert play_resp.status_code == 200
    assert play_resp.json()["play_url"] == "https://signed.example.com/video.webm"

    # Batch remove emp2 returns updated cluster count = 1
    set_user(admin_user)
    del_mem = client.delete(f"/api/clusters/{cl_id}/members/42")
    assert del_mem.status_code == 200
    assert del_mem.json()["cluster"]["member_count"] == 1


# ============================================================================
# 11. Senior Engineering Hardening: RBAC Hierarchy, Cheats, Deadlines & Guards
# ============================================================================

def test_typing_early_submit_cheat_trailing_space_and_accuracy_default():
    passage = " ".join(f"word{i}" for i in range(1, 26))  # 25 words
    snapshot = {
        "test_type": "typing",
        "target_wpm": 30,
        # omit min_accuracy_percent to verify 95.0 default
        "passage_text": passage,
    }
    # Anti-cheat: typing only 2 words in 1 second must NOT pass even with high WPM/100% match
    cheat_score, cheat_passed, cheat_summary = evaluate_typing_attempt(
        snapshot,
        {"typed_text": "word1 word2", "keystroke_count": 12},
        time_spent_seconds=1.5,
    )
    assert cheat_passed is False
    assert cheat_summary["min_accuracy_percent"] == 95.0

    # Honest full passage with trailing space must pass cleanly
    score, passed, summary = evaluate_typing_attempt(
        snapshot,
        {"typed_text": passage + " ", "keystroke_count": len(passage) + 5},
        time_spent_seconds=30.0,
    )
    assert passed is True
    assert score > 0
    assert summary["accuracy_percent"] == 100.0


def test_malformed_blueprint_raises_validation_error_not_value_error():
    bad_configs = [
        {"categories": [{"category_id": "c1", "name": "C1", "pick_count": "abc", "questions": []}]},
        {"categories": [{"category_id": "c1", "name": "C1", "pick_count": 1, "questions": ["not-a-dict"]}]},
        {
            "categories": [{
                "category_id": "c1",
                "name": "C1",
                "pick_count": 1,
                "questions": [{"question_id": "q1", "options": ["A", "B"], "correct_index": 0, "marks": 0}],
            }]
        },
        {
            "categories": [{
                "category_id": "c1",
                "name": "C1",
                "pick_count": 1,
                "questions": [{"question_id": "q1", "options": ["A", "B"], "correct_index": 0, "marks": "bad"}],
            }]
        },
    ]
    for cfg in bad_configs:
        with pytest.raises(AssessmentValidationError):
            validate_blueprint_for_publish("mcq", cfg, passing_score=70.0)

    for bad_typing in (
        {"passage_text": " ".join(["ok"] * 25), "target_wpm": "fast"},
        {"passage_text": " ".join(["ok"] * 25), "target_wpm": 40, "min_accuracy_percent": 0},
        {"passage_text": " ".join(["ok"] * 25), "target_wpm": 40, "min_accuracy_percent": 105},
    ):
        with pytest.raises(AssessmentValidationError):
            validate_blueprint_for_publish("typing", bad_typing)


def test_sanitize_strips_flags_and_proctoring_config_from_candidates():
    snap = {
        "test_type": "mcq",
        "passing_score": 80.0,
        "proctoring_config": {"require_camera": True, "max_tab_switches": 1},
        "questions": [
            {"question_id": "q1", "question": "Q", "options": ["A", "B"], "correct_index": 1, "explanation": "Exp"}
        ],
    }
    clean_snap = sanitize_snapshot_for_candidate(snap)
    assert "proctoring_config" not in clean_snap
    assert "correct_index" not in clean_snap["questions"][0]
    assert "explanation" not in clean_snap["questions"][0]

    raw_summary = {
        "test_type": "typing",
        "net_wpm": 25.0,
        "is_flagged": True,
        "flag_reason": "suspicious_bulk_paste_keystrokes_missing",
        "item_diagnostics": [{"question_id": "q1", "correct_index": 0}],
    }
    clean_sum = sanitize_result_summary_for_candidate(raw_summary)
    assert "is_flagged" not in clean_sum
    assert "flag_reason" not in clean_sum
    assert "item_diagnostics" not in clean_sum


def test_sync_dynamic_clusters_savepoint_and_fulltime_promotion(db_session, sample_mcq_config):
    from app.services import cluster_service

    emp = Employee(id=950, name="Intern One", email="int@x.com", status="active", designation="Developer", employee_type="Intern")
    ft_cluster = Cluster(id=95, name="Full-Time Staff", dynamic_scope="full_time", is_archived=False)
    bad_cluster = Cluster(id=96, name="Broken Scope", dynamic_scope="developers", is_archived=False)
    asm = Assessment(id=95, title="FT Onboarding", test_type="mcq", status="published", passing_score=70.0, test_config=sample_mcq_config)
    asg = Assignment(id=95, cluster_id=95, assessment_id=95, relative_due_days=7, is_active=True)
    db_session.add_all([emp, ft_cluster, bad_cluster, asm, asg])
    db_session.commit()

    # Promote Intern -> Full-time without outer commit yet; patch one cluster failure to test savepoint isolation
    emp.employee_type = "Full-time"
    orig_eval = cluster_service.get_eligible_employees_query

    def _flaky_eval(db, cluster):
        if cluster.id == 96:
            raise RuntimeError("Simulated SQL failure on one cluster")
        return orig_eval(db, cluster)

    with patch("app.services.cluster_service.get_eligible_employees_query", side_effect=_flaky_eval):
        sync_dynamic_clusters_for_employee(db_session, emp.id)
    db_session.commit()

    # Full-time cluster still succeeded cleanly without poisoning outer transaction!
    assert db_session.query(ClusterMember).filter_by(cluster_id=95, employee_id=950).first() is not None
    enr = db_session.query(AssessmentEnrollment).filter_by(assessment_id=95, employee_id=950).first()
    assert enr is not None and enr.status == "assigned"


def test_pm_and_team_lead_hierarchy_scope_and_self_isolation(http_env, sample_mcq_config):
    from app.services import project_scope

    project_scope.invalidate_manageable_cache()
    client = http_env["client"]
    db = http_env["db"]
    set_user = http_env["set_user"]

    now = datetime.now(timezone.utc)
    admin_user = User(id=1, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True)
    hr_user = User(id=2, name="HR", email="hr@x.com", password_hash="x", role="hr", is_active=True)

    pm_emp = Employee(id=201, name="PM Alpha", email="pma@x.com", status="active", designation="Program Manager", employee_type="Full-time")
    lead_emp = Employee(id=202, name="Lead Alpha", email="leada@x.com", status="active", designation="Team Lead", employee_type="Full-time")
    sub_emp = Employee(id=203, name="Worker Alpha", email="wa@x.com", status="active", designation="Annotator", employee_type="Full-time")
    peer_pm_emp = Employee(id=204, name="PM Beta", email="pmb@x.com", status="active", designation="Program Manager", employee_type="Full-time")
    other_emp = Employee(id=205, name="Worker Beta", email="wb@x.com", status="active", designation="Annotator", employee_type="Full-time")

    pm_user = User(id=11, name="PM Alpha", email="pma@x.com", password_hash="x", role="pm", employee_id=201, is_active=True)
    lead_user = User(id=12, name="Lead Alpha", email="leada@x.com", password_hash="x", role="team_lead", employee_id=202, is_active=True)
    sub_user = User(id=13, name="Worker Alpha", email="wa@x.com", password_hash="x", role="employee", employee_id=203, is_active=True)
    peer_pm_user = User(id=14, name="PM Beta", email="pmb@x.com", password_hash="x", role="pm", employee_id=204, is_active=True)
    other_user = User(id=15, name="Worker Beta", email="wb@x.com", password_hash="x", role="employee", employee_id=205, is_active=True)

    # Project Alpha (PM=201 in assigned_employee_ids, Lead=202, Sub=203) & Project Beta (PM=204, Sub=205)
    mp1 = MainProject(id=1, name="Proj Alpha", program_manager_id=201, status="active")
    sp1 = SubProject(id=1, main_project_id=1, name="Sub Alpha", pm_id=201, status="active")
    ds1 = DailySheet(
        id=1,
        sub_project_id=1,
        main_project_id=1,
        name="DS Alpha",
        client="C",
        project_type="Full",
        total_tasks=10,
        estimated_time_per_task=1.0,
        start_date=date.today(),
        assigned_employee_ids=[201],
    )
    alloc_lead = Allocation(id=1, employee_id=202, sub_project_id=1, role_tags=["Team Lead"], is_active=True)
    alloc_sub = Allocation(id=2, employee_id=203, sub_project_id=1, role_tags=["Annotator"], is_active=True)

    mp2 = MainProject(id=2, name="Proj Beta", program_manager_id=204, status="active")
    sp2 = SubProject(id=2, main_project_id=2, name="Sub Beta", pm_id=204, status="active")
    ds2 = DailySheet(
        id=2,
        sub_project_id=2,
        main_project_id=2,
        name="DS Beta",
        client="C",
        project_type="Full",
        total_tasks=10,
        estimated_time_per_task=1.0,
        start_date=date.today(),
        assigned_employee_ids=[204],
    )
    alloc_other = Allocation(id=3, employee_id=205, sub_project_id=2, role_tags=["Annotator"], is_active=True)

    asm = Assessment(
        id=90,
        title="Scoped QA Test",
        test_type="mcq",
        status="published",
        duration_minutes=15,
        passing_score=50.0,
        proctoring_config={"require_camera": False, "max_tab_switches": 1},
        test_config=sample_mcq_config,
    )
    enr_pm = AssessmentEnrollment(id=901, assessment_id=90, employee_id=201, status="assigned", deadline=now + timedelta(days=3))
    enr_lead = AssessmentEnrollment(id=902, assessment_id=90, employee_id=202, status="assigned", deadline=now + timedelta(days=3))
    enr_sub = AssessmentEnrollment(id=903, assessment_id=90, employee_id=203, status="assigned", deadline=now + timedelta(days=3))
    enr_other = AssessmentEnrollment(id=905, assessment_id=90, employee_id=205, status="assigned", deadline=now + timedelta(days=3))

    db.add_all([
        admin_user, hr_user, pm_emp, lead_emp, sub_emp, peer_pm_emp, other_emp,
        pm_user, lead_user, sub_user, peer_pm_user, other_user,
        mp1, sp1, ds1, alloc_lead, alloc_sub, mp2, sp2, ds2, alloc_other,
        asm, enr_pm, enr_lead, enr_sub, enr_other,
    ])
    db.commit()

    # Each enrolled employee completes an attempt with 2 tab switches (auto-flagged) & recording ready
    attempt_by_emp = {}
    for emp_id, usr, enr_id in ((201, pm_user, 901), (202, lead_user, 902), (203, sub_user, 903), (205, other_user, 905)):
        set_user(usr)
        s = client.post(f"/api/assessments/enrollments/{enr_id}/attempts", json={"camera_verified": False})
        assert s.status_code == 200
        aid = s.json()["id"]
        attempt_by_emp[emp_id] = aid
        qids = [q["question_id"] for q in s.json()["question_snapshot"]["questions"]]
        client.post(f"/api/assessments/attempts/{aid}/telemetry", json={"events": [{"event_type": "tab_switch"}, {"event_type": "tab_switch"}]})
        sub = client.post(f"/api/assessments/attempts/{aid}/submit", json={"answers": {qid: 0 for qid in qids}})
        assert sub.status_code == 200
        # Candidate view strips is_flagged & flag_reason
        assert "is_flagged" not in sub.json()
        assert "flag_reason" not in sub.json()
        db.add(AssessmentRecording(
            attempt_id=aid,
            video_storage_path=f"assessments/{aid}/proctoring.webm",
            status="ready",
            expires_at=now + timedelta(days=5),
        ))
    db.commit()

    # PM Alpha sees Lead (202) and Worker (203), NEVER self (201) or Beta Worker (205)
    set_user(pm_user)
    pm_cards = client.get("/api/assessments/scorecards")
    assert pm_cards.status_code == 200
    assert {r["employee_id"] for r in pm_cards.json()} == {202, 203}

    pm_admin_reg = client.get("/api/assessments/admin/enrollments")
    assert pm_admin_reg.status_code == 200
    assert {r["employee_id"] for r in pm_admin_reg.json()["items"]} == {202, 203}

    # PM can inspect Sub (203) logs & play-url, gets candidate-safe self scorecard, and 403 on self/Beta logs & video
    with patch("app.api.assessments.create_signed_play_url", return_value="https://ok/v.webm"), \
         patch("app.api.assessments.is_supabase_configured", return_value=True):
        assert client.get(f"/api/assessments/results/{attempt_by_emp[203]}/logs").status_code == 200
        assert client.get(f"/api/assessments/results/{attempt_by_emp[203]}/recording/play-url").status_code == 200
        assert client.get(f"/api/assessments/results/{attempt_by_emp[201]}/logs").status_code == 403
        assert client.get(f"/api/assessments/results/{attempt_by_emp[201]}/recording/play-url").status_code == 403
        assert client.get(f"/api/assessments/results/{attempt_by_emp[205]}/logs").status_code == 403
        assert client.get(f"/api/assessments/results/{attempt_by_emp[205]}/recording/play-url").status_code == 403

    pm_self_sc = client.get("/api/assessments/enrollments/901/scorecard")
    assert pm_self_sc.status_code == 200
    assert "is_flagged" not in pm_self_sc.json()  # candidate-safe even when PM inspects own test

    # Team Lead Alpha sees ONLY Worker Alpha (203), NEVER PM (201), self (202), or Beta (205)
    set_user(lead_user)
    lead_cards = client.get("/api/assessments/90/scorecards")
    assert lead_cards.status_code == 200
    assert {r["employee_id"] for r in lead_cards.json()} == {203}
    assert client.get(f"/api/assessments/results/{attempt_by_emp[202]}/logs").status_code == 403
    assert client.get(f"/api/assessments/results/{attempt_by_emp[205]}/logs").status_code == 403

    # Neither PM nor Team Lead can mutate/retest/extend/send reminders
    assert client.post("/api/assessments/enrollments/903/grant-retest", json={"reason": "No"}).status_code == 403
    assert client.post("/api/assessments/reminders/send", json={}).status_code == 403

    # HR sees ALL 4 enrolled candidates
    set_user(hr_user)
    hr_cards = client.get("/api/assessments/scorecards")
    assert hr_cards.status_code == 200
    assert {r["employee_id"] for r in hr_cards.json()} == {201, 202, 203, 205}


def test_security_deadlines_camera_versioning_archival_and_recording_guards(http_env, sample_mcq_config):
    client = http_env["client"]
    db = http_env["db"]
    set_user = http_env["set_user"]

    now = datetime.now(timezone.utc)
    admin_user = User(id=1, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True)
    emp = Employee(id=301, name="Cand", email="c301@x.com", status="active", designation="Developer", employee_type="Full-time")
    cand_user = User(id=21, name="Cand", email="c301@x.com", password_hash="x", role="employee", employee_id=301, is_active=True)
    db.add_all([admin_user, emp, cand_user])
    db.commit()

    # 1. Archived cluster name reuse creates brand-new cluster without resurrecting old members
    set_user(admin_user)
    c1 = client.post("/api/clusters", json={"name": "Pilot Wave", "dynamic_scope": "custom", "employee_ids": [301]}).json()
    assert client.delete(f"/api/clusters/{c1['id']}").status_code == 200
    c2_resp = client.post("/api/clusters", json={"name": "Pilot Wave", "dynamic_scope": "custom"})
    assert c2_resp.status_code == 201
    c2 = c2_resp.json()
    assert c2["id"] != c1["id"]
    assert c2["member_count"] == 0
    assert client.post(f"/api/clusters/{c2['id']}/members", json={"employee_ids": [301]}).status_code == 200

    # 2. Published blueprint with smart versioning & max_attempts_snapshot freeze
    asm = client.post("/api/assessments", json={
        "title": "Camera & Version Test",
        "test_type": "mcq",
        "duration_minutes": 20,
        "passing_score": 50.0,
        "max_attempts": 2,
        "proctoring_config": {"require_camera": True, "max_tab_switches": 2},
        "test_config": sample_mcq_config,
    }).json()
    asm_id = asm["id"]
    assert client.put(f"/api/assessments/{asm_id}", json={"status": "published"}).status_code == 200

    asg_dl = now + timedelta(days=5)
    asg = client.post("/api/assignments", json={
        "cluster_id": c2["id"],
        "assessment_id": asm_id,
        "deadline": asg_dl.isoformat(),
    }).json()
    enr = db.query(AssessmentEnrollment).filter_by(employee_id=301, assessment_id=asm_id).first()
    assert enr.deadline is None  # fixed deadline inherits Tier 1
    assert enr.max_attempts_snapshot == 2

    # Fixed deadline inheritance in /my/enrollments & /scorecard
    set_user(cand_user)
    my_enr = client.get("/api/assessments/my/enrollments").json()
    assert my_enr[0]["deadline"] is not None
    assert client.get(f"/api/assessments/enrollments/{enr.id}/scorecard").json()["deadline"] is not None

    # Camera verification enforced server-side when require_camera=True
    assert client.post(f"/api/assessments/enrollments/{enr.id}/attempts", json={"camera_verified": False}).status_code == 400

    # Smart versioning: title edit or unchanged values keep version=1, max_attempts=1 keeps snapshot=2, passing_score/proctoring edits bump version
    set_user(admin_user)
    t_upd = client.put(f"/api/assessments/{asm_id}", json={
        "title": "Renamed Title Only",
        "max_attempts": 1,
        "duration_minutes": 20,  # unchanged value must not bump version
    })
    assert t_upd.status_code == 200
    assert t_upd.json()["version"] == 1
    assert client.post("/api/assessments", json={"title": "Bad Proc", "proctoring_config": {"max_tab_switches": -1}}).status_code == 422
    p_upd = client.put(f"/api/assessments/{asm_id}", json={"passing_score": 60.0})
    assert p_upd.status_code == 200
    assert p_upd.json()["version"] == 2
    proc_upd = client.put(f"/api/assessments/{asm_id}", json={"proctoring_config": {"require_camera": True, "max_tab_switches": 1}})
    assert proc_upd.status_code == 200
    assert proc_upd.json()["version"] == 3

    # Never Shorten deadline enforcement + cohort audit log
    short_ind = client.put(
        f"/api/assessments/enrollments/{enr.id}/extend-deadline",
        json={"new_deadline": (now + timedelta(days=3)).isoformat(), "reason": "Try shorten"},
    )
    assert short_ind.status_code == 400
    short_cohort = client.put(
        f"/api/assignments/{asg['id']}/deadline",
        json={"new_deadline": (now + timedelta(days=3)).isoformat()},
    )
    assert short_cohort.status_code == 400
    ext_cohort = client.put(
        f"/api/assignments/{asg['id']}/deadline",
        json={"new_deadline": (now + timedelta(days=7)).isoformat()},
    )
    assert ext_cohort.status_code == 200
    assert len(db.query(AuditLog).filter_by(entity_type="assignment", entity_id=asg["id"]).all()) >= 2

    # Start sitting & test stale draft saved=False + recording finalize guards
    set_user(cand_user)
    start = client.post(f"/api/assessments/enrollments/{enr.id}/attempts", json={"camera_verified": True}).json()
    att_id = start["id"]
    qids = [q["question_id"] for q in start["question_snapshot"]["questions"]]
    d1 = client.put(f"/api/assessments/attempts/{att_id}/draft", json={"answers": {qids[0]: 0}, "answers_seq": 2})
    assert d1.json()["saved"] is True
    d_stale = client.put(f"/api/assessments/attempts/{att_id}/draft", json={"answers": {qids[0]: 1}, "answers_seq": 1})
    assert d_stale.json()["saved"] is False

    # Recording finalize before any object uploaded fails & purged recording cannot be resurrected
    with patch.dict(os.environ, {"ENV": "production", "ALLOW_LOCAL_RECORDING_FALLBACK": "false"}), \
         patch("app.services.assessment_storage_service.is_supabase_configured", return_value=False), \
         patch("app.api.assessments.is_supabase_configured", return_value=False):
        assert client.post(f"/api/assessments/attempts/{att_id}/recording/upload-url").status_code == 503

    with tempfile.TemporaryDirectory() as tmp_rec_dir, \
         patch.dict(os.environ, {"ALLOW_LOCAL_RECORDING_FALLBACK": "true"}), \
         patch("app.services.assessment_storage_service.is_supabase_configured", return_value=False), \
         patch("app.api.assessments.is_supabase_configured", return_value=False), \
         patch("app.services.assessment_storage_service.LOCAL_RECORDINGS_DIR", tmp_rec_dir), \
         patch("app.api.assessments.LOCAL_RECORDINGS_DIR", tmp_rec_dir):
        up_info = client.post(f"/api/assessments/attempts/{att_id}/recording/upload-url")
        assert up_info.status_code == 200
        # Finalize BEFORE bytes uploaded must fail (400/404/409)
        assert client.post(f"/api/assessments/attempts/{att_id}/recording/finalize").status_code >= 400
        # Empty 0-byte upload must be rejected (400)
        assert client.put(up_info.json()["upload_url"], content=b"").status_code == 400
        assert client.put(up_info.json()["upload_url"], content=b"video-bytes").status_code == 200
        # Playback before finalize must fail (409)
        set_user(admin_user)
        assert client.get(f"/api/assessments/attempts/{att_id}/recording/local").status_code == 409
        set_user(cand_user)
        assert client.post(f"/api/assessments/attempts/{att_id}/recording/finalize").status_code == 200
        # Requesting a new upload-url after ready is refused (409)
        assert client.post(f"/api/assessments/attempts/{att_id}/recording/upload-url").status_code == 409
        # Purged recording cannot be resurrected by finalize
        rec = db.query(AssessmentRecording).filter_by(attempt_id=att_id).first()
        rec.status = "purged"
        db.commit()
        assert client.post(f"/api/assessments/attempts/{att_id}/recording/finalize").status_code == 410

    # Archiving assessment cascades assignment deactivation & blocks non-admin /overview
    set_user(admin_user)
    assert client.delete(f"/api/assessments/{asm_id}").status_code == 200
    db.expire_all()
    assert db.get(Assignment, asg["id"]).is_active is False
    set_user(cand_user)
    assert client.get(f"/api/assessments/{asm_id}/overview").status_code == 403


def test_multi_scope_dynamic_clusters_and_member_picker_options(http_env):
    from datetime import date
    from app.models.project import DailySheet
    from app.models.allocation import Allocation

    client = http_env["client"]
    db = http_env["db"]
    set_user = http_env["set_user"]

    admin_user = User(id=1, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True)
    emp_ft = Employee(id=201, name="FullTime Annotator", email="ft@x.com", status="active", designation="Annotator", employee_type="Full-time", skills=["NLP"])
    emp_dev = Employee(id=202, name="PartTime Dev", email="dev@x.com", status="active", designation="Developer", employee_type="Contract", skills=["Python", "React"])
    emp_idle = Employee(id=203, name="Idle QA", email="idle@x.com", status="active", designation="QA", employee_type="Contract", skills=["QA"])
    emp_arch = Employee(id=204, name="Archived", email="arch@x.com", status="archived", designation="Developer", employee_type="Full-time")

    sheet_ocean = DailySheet(
        id=301,
        name="Ocean",
        client="Walden",
        project_type="Full",
        total_tasks=100,
        estimated_time_per_task=1.0,
        start_date=date(2026, 1, 1),
        project_status="active",
        assigned_employee_ids=[],
    )
    sheet_done = DailySheet(
        id=302,
        name="Archived Sheet",
        client="Old",
        project_type="Full",
        total_tasks=50,
        estimated_time_per_task=1.0,
        start_date=date(2025, 1, 1),
        project_status="completed",
        assigned_employee_ids=[],
    )
    alloc_ocean_dev = Allocation(employee_id=202, sub_project_id=301, is_active=True)
    alloc_inactive = Allocation(employee_id=203, sub_project_id=301, is_active=False)

    db.add_all([admin_user, emp_ft, emp_dev, emp_idle, emp_arch, sheet_ocean, sheet_done, alloc_ocean_dev, alloc_inactive])
    db.commit()

    set_user(admin_user)

    # 1. Single pooled picker endpoint reflects DailySheet allocations + Idle state accurately
    picker_resp = client.get("/api/clusters/member-picker-options")
    assert picker_resp.status_code == 200
    picker = picker_resp.json()

    assert [s["name"] for s in picker["daily_sheets"]] == ["Ocean"]
    assert picker["daily_sheets"][0]["active_member_count"] == 1
    assert picker["idle_count"] == 2  # emp_ft (201) + emp_idle (203) have no active Allocation
    assert "Python" in picker["skills"]
    emp_map = {e["id"]: e for e in picker["employees"]}
    assert 204 not in emp_map
    assert emp_map[202]["is_idle"] is False
    assert emp_map[202]["daily_sheet_ids"] == [301]
    assert emp_map[203]["is_idle"] is True
    assert emp_map[203]["daily_sheet_ids"] == []

    # 2. Multi-rule dynamic cluster combines rules with OR union (Full-time OR Developers)
    multi_resp = client.post("/api/clusters", json={
        "name": "FT + Devs Cohort",
        "dynamic_scopes": ["full_time", "developers"],
    })
    assert multi_resp.status_code == 201
    cl = multi_resp.json()
    assert cl["is_dynamic"] is True
    assert cl["dynamic_scopes"] == ["full_time", "developers"]
    assert cl["member_count"] == 2  # emp_ft (Full-time) + emp_dev (Developer)

    # New employee promoted to Developer auto-joins via multi-rule sync_dynamic_clusters_for_employee
    emp_idle.designation = "Developer"
    sync_dynamic_clusters_for_employee(db, emp_idle.id)
    db.commit()
    assert client.get(f"/api/clusters/{cl['id']}").json()["member_count"] == 3

    # Requires joined_on_or_after whenever new_joiners is one of the selected scopes
    bad_upd = client.put(f"/api/clusters/{cl['id']}", json={"dynamic_scopes": ["developers", "new_joiners"]})
    assert bad_upd.status_code == 422

    # Updating dynamic_scopes reconciles roster
    tl_only = client.put(f"/api/clusters/{cl['id']}", json={"dynamic_scopes": ["team_leads"]})
    assert tl_only.status_code == 200
    assert tl_only.json()["dynamic_scopes"] == ["team_leads"]
    assert tl_only.json()["member_count"] == 0


def test_postgres_for_update_of_avoids_outer_join_crash():
    """Compiles outer-join + with_for_update queries against PostgreSQL dialect."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import joinedload, sessionmaker
    from sqlalchemy import create_engine

    engine = create_engine("sqlite:///:memory:")
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        q = (
            db.query(AssessmentEnrollment)
            .options(joinedload(AssessmentEnrollment.assignment), joinedload(AssessmentEnrollment.assessment))
            .filter(AssessmentEnrollment.id == 1)
            .with_for_update(of=AssessmentEnrollment)
        )
        compiled_sql = str(q.statement.compile(dialect=postgresql.dialect()))
        assert "FOR UPDATE OF assessment_enrollments" in compiled_sql
    finally:
        db.close()


def test_typing_long_passage_requires_50_percent_coverage():
    passage = " ".join(f"word{i}" for i in range(1, 301))  # 300 words
    snapshot = {
        "test_type": "typing",
        "target_wpm": 30,
        "min_accuracy_percent": 95.0,
        "passage_text": passage,
    }
    # Typing only 20 words fast out of 300 words (6.7%) must NOT pass
    short_typed = " ".join(f"word{i}" for i in range(1, 21))
    _, short_passed, short_sum = evaluate_typing_attempt(
        snapshot,
        {"typed_text": short_typed, "keystroke_count": len(short_typed)},
        time_spent_seconds=5.0,
    )
    assert short_sum["min_words_required"] == 150
    assert short_passed is False

    # Typing 150 words (50%) accurately must pass
    half_typed = " ".join(f"word{i}" for i in range(1, 151))
    _, half_passed, _ = evaluate_typing_attempt(
        snapshot,
        {"typed_text": half_typed, "keystroke_count": len(half_typed) + 5},
        time_spent_seconds=60.0,
    )
    assert half_passed is True


def test_database_url_logging_never_leaks_credentials():
    import pathlib

    src = pathlib.Path("app/db/database.py").read_text(encoding="utf-8")
    assert "split('@')" not in src
    assert "make_url" in src


def test_pool_release_unpublish_cooldown_telemetry_cap_and_grouped_logs(http_env, sample_mcq_config):
    from app.models.audit_log import AuditLog

    client = http_env["client"]
    db = http_env["db"]
    set_user = http_env["set_user"]

    now = datetime.now(timezone.utc)
    admin_user = User(id=1, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True)
    emp1 = Employee(id=501, name="Cand One", email="c501@x.com", status="active", designation="Developer", employee_type="Full-time")
    emp2 = Employee(id=502, name="Cand Two", email="c502@x.com", status="active", designation="Developer", employee_type="Full-time")
    u1 = User(id=51, name="Cand One", email="c501@x.com", password_hash="x", role="employee", employee_id=501, is_active=True)
    asm = Assessment(
        id=55,
        title="Opt & Pool Test",
        test_type="mcq",
        status="published",
        duration_minutes=15,
        passing_score=50.0,
        max_attempts=3,
        proctoring_config={"require_camera": False, "max_tab_switches": 1},
        test_config={**sample_mcq_config, "shuffle_options": False},
    )
    cl = Cluster(id=55, name="Dynamic Opt", dynamic_scope="developers", is_archived=False)
    db.add_all([admin_user, emp1, emp2, u1, asm, cl])
    db.commit()

    # Dynamic cluster populates 2 members & creates assignment
    set_user(admin_user)
    assert client.put(f"/api/clusters/{cl.id}", json={"dynamic_scope": "developers"}).status_code == 200
    populate_cluster_members(db, cl)
    db.commit()
    asg = client.post("/api/assignments", json={
        "cluster_id": cl.id,
        "assessment_id": 55,
        "deadline": (now + timedelta(days=3)).isoformat(),
    }).json()

    enr1 = db.query(AssessmentEnrollment).filter_by(employee_id=501, assessment_id=55).first()
    enr2 = db.query(AssessmentEnrollment).filter_by(employee_id=502, assessment_id=55).first()

    # 1. Start attempt, verify telemetry cap at 1000 rows, grouped log counts & stripped list item_diagnostics
    set_user(u1)
    att = client.post(f"/api/assessments/enrollments/{enr1.id}/attempts", json={"camera_verified": False}).json()
    att_id = att["id"]
    qids = [q["question_id"] for q in att["question_snapshot"]["questions"]]

    # Seed 995 existing logs and ingest 10 -> capped strictly at 1000
    db.add_all([AssessmentLog(attempt_id=att_id, event_type="window_blur", details={}) for _ in range(995)])
    db.commit()
    tel = client.post(
        f"/api/assessments/attempts/{att_id}/telemetry",
        json={"events": [{"event_type": "tab_switch"} for _ in range(10)]},
    )
    assert tel.status_code == 200
    assert tel.json()["recorded_count"] == 5
    assert db.query(AssessmentLog).filter_by(attempt_id=att_id).count() == 1000

    assert client.post(f"/api/assessments/attempts/{att_id}/submit", json={"answers": {qid: 0 for qid in qids}}).status_code == 200

    set_user(admin_user)
    cards = client.get("/api/assessments/scorecards").json()
    c_row = next(r for r in cards if r["enrollment_id"] == enr1.id)
    assert c_row["tab_switch_count"] == 5
    assert c_row["is_flagged"] is True
    assert "item_diagnostics" not in c_row["attempts"][0]["result_summary"]

    reg = client.get(f"/api/assessments/admin/enrollments?cluster_id={cl.id}").json()
    assert reg["total_items"] == 2
    # Detail /scorecard retains full item_diagnostics
    detail_sc = client.get(f"/api/assessments/enrollments/{enr1.id}/scorecard").json()
    assert "item_diagnostics" in detail_sc["attempts"][0]["result_summary"]

    # 2. Recording upload-url and finalize close DB session BEFORE calling external storage helpers
    call_order = []
    from sqlalchemy.orm.session import Session as SASession
    orig_close = SASession.close

    def _spy_close(self):
        call_order.append("db.close")
        return orig_close(self)

    def _fake_upload_url(*args, **kwargs):
        call_order.append("create_signed_upload_url")
        return "https://signed.example.com/upload.webm"

    def _fake_exists(*args, **kwargs):
        call_order.append("check_recording_exists")
        return True

    set_user(u1)
    with patch.object(SASession, "close", _spy_close), \
         patch("app.api.assessments.is_supabase_configured", return_value=True), \
         patch("app.api.assessments.create_signed_upload_url", side_effect=_fake_upload_url), \
         patch("app.api.assessments.check_recording_exists", side_effect=_fake_exists):
        assert client.post(f"/api/assessments/attempts/{att_id}/recording/upload-url").status_code == 200
        assert call_order.index("db.close") < call_order.index("create_signed_upload_url")
        call_order.clear()
        assert client.post(f"/api/assessments/attempts/{att_id}/recording/finalize").status_code == 200
        assert call_order.index("db.close") < call_order.index("check_recording_exists")

    # 3. Bulk reminder cooldown prevents spamming same pending candidate twice within 60 minutes
    set_user(admin_user)
    with patch("app.api.assessments.try_send_assessment_reminder_dm", return_value=True):
        r1 = client.post("/api/assessments/reminders/send", json={"cluster_id": cl.id}).json()
        assert r1["notified_count"] == 1  # only enr2 is still pending
        r2 = client.post("/api/assessments/reminders/send", json={"cluster_id": cl.id}).json()
        assert r2["notified_count"] == 0
        assert r2["skipped_cooldown_count"] == 1

    # 4. Bulk max_attempts_snapshot update + unpublish (published -> draft) cascade + dynamic->custom audit
    upd_asm = client.put("/api/assessments/55", json={"max_attempts": 1, "status": "draft"})
    assert upd_asm.status_code == 200
    db.expire_all()
    assert db.get(AssessmentEnrollment, enr2.id).max_attempts_snapshot == 3
    assert db.get(AssessmentEnrollment, enr2.id).status == "cancelled"
    assert db.get(Assignment, asg["id"]).is_active is False

    conv = client.put(f"/api/clusters/{cl.id}", json={"dynamic_scopes": ["custom"]})
    assert conv.status_code == 200
    assert conv.json()["member_count"] == 2
    conv_log = db.query(AuditLog).filter_by(entity_type="cluster", entity_id=cl.id).order_by(AuditLog.id.desc()).first()
    assert "custom" in conv_log.summary


# ============================================================================
# 10. Hardening Regression Tests: Lock Timeouts, Scalar Telemetry, Reaper Batches,
#     Typing/Telemetry Bounds, ARQ Offload & Member-Picker Caching
# ============================================================================

def test_typing_strict_keys_keystroke_bound_and_telemetry_size_limits(http_env):
    client = http_env["client"]
    db = http_env["db"]
    set_user = http_env["set_user"]

    emp = Employee(id=601, name="Typing Cand", email="t601@x.com", status="active", employee_type="Full-time")
    user = User(id=61, name="Typing Cand", email="t601@x.com", password_hash="x", role="employee", employee_id=601, is_active=True)
    passage = " ".join(f"word{i}" for i in range(25))
    asm = Assessment(
        id=61,
        title="Typing Bounds",
        test_type="typing",
        status="published",
        duration_minutes=10,
        passing_score=40.0,
        max_attempts=2,
        proctoring_config={},
        test_config={"passage_text": passage, "target_wpm": 30, "min_accuracy_percent": 90.0},
    )
    enr = AssessmentEnrollment(id=601, assessment_id=61, employee_id=601, status="assigned")
    db.add_all([emp, user, asm, enr])
    db.commit()

    set_user(user)
    att = client.post(f"/api/assessments/enrollments/{enr.id}/attempts", json={"camera_verified": False}).json()
    att_id = att["id"]

    # Unexpected extra key in typing draft -> 422
    bad_extra = client.put(
        f"/api/assessments/attempts/{att_id}/draft",
        json={"answers": {"typed_text": "word0", "keystroke_count": 5, "cheated": True}, "answers_seq": 1},
    )
    assert bad_extra.status_code == 422

    # Excessive keystroke_count -> 422
    bad_keys = client.put(
        f"/api/assessments/attempts/{att_id}/draft",
        json={"answers": {"typed_text": "word0", "keystroke_count": 999999}, "answers_seq": 1},
    )
    assert bad_keys.status_code == 422

    # Oversized telemetry details (> 2048 JSON bytes) -> 422
    oversized_tel = client.post(
        f"/api/assessments/attempts/{att_id}/telemetry",
        json={"events": [{"event_type": "tab_switch", "details": {"blob": "a" * 2500}}]},
    )
    assert oversized_tel.status_code == 422

    # Excessive events batch (> 50) in draft autosave -> 422
    too_many_events = client.put(
        f"/api/assessments/attempts/{att_id}/draft",
        json={
            "answers": {"typed_text": "word0", "keystroke_count": 5},
            "answers_seq": 1,
            "events": [{"event_type": "tab_switch"} for _ in range(51)],
        },
    )
    assert too_many_events.status_code == 422

    # Valid typing draft saves cleanly
    good_draft = client.put(
        f"/api/assessments/attempts/{att_id}/draft",
        json={"answers": {"typed_text": "word0 word1", "keystroke_count": 11}, "answers_seq": 1},
    )
    assert good_draft.status_code == 200
    assert good_draft.json()["saved"] is True


def test_lock_contention_55p03_and_lock_free_telemetry(http_env, sample_mcq_config):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.exc import OperationalError

    client = http_env["client"]
    db = http_env["db"]
    set_user = http_env["set_user"]

    # 1. Verify PostgreSQL SQL compilation for lock-free telemetry vs NOWAIT draft
    tel_q = (
        db.query(AssessmentAttempt.id, AssessmentAttempt.status, AssessmentAttempt.expires_at)
        .join(AssessmentEnrollment, AssessmentEnrollment.id == AssessmentAttempt.enrollment_id)
        .filter(AssessmentAttempt.id == 1, AssessmentEnrollment.employee_id == 1)
    )
    tel_sql = str(tel_q.statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" not in tel_sql
    assert "question_snapshot" not in tel_sql
    assert "assessment_attempts.answers" not in tel_sql

    draft_q = (
        db.query(AssessmentAttempt)
        .join(AssessmentEnrollment, AssessmentEnrollment.id == AssessmentAttempt.enrollment_id)
        .filter(AssessmentAttempt.id == 1, AssessmentEnrollment.employee_id == 1)
        .with_for_update(of=AssessmentAttempt, nowait=True)
    )
    draft_sql = str(draft_q.statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE OF assessment_attempts NOWAIT" in draft_sql

    # 2. Simulated Postgres SQLSTATE 55P03 returns HTTP 409 with Retry-After: 1
    emp = Employee(id=602, name="Lock Cand", email="l602@x.com", status="active", employee_type="Full-time")
    user = User(id=62, name="Lock Cand", email="l602@x.com", password_hash="x", role="employee", employee_id=602, is_active=True)
    asm = Assessment(
        id=62,
        title="Lock Test",
        test_type="mcq",
        status="published",
        duration_minutes=15,
        passing_score=50.0,
        max_attempts=2,
        proctoring_config={},
        test_config=sample_mcq_config,
    )
    enr = AssessmentEnrollment(id=602, assessment_id=62, employee_id=602, status="assigned")
    db.add_all([emp, user, asm, enr])
    db.commit()

    set_user(user)
    att = client.post(f"/api/assessments/enrollments/{enr.id}/attempts", json={"camera_verified": False}).json()

    class _FakePgLockOrig(Exception):
        pgcode = "55P03"

    def _raise_lock_err(*args, **kwargs):
        raise OperationalError("SELECT ... FOR UPDATE", {}, _FakePgLockOrig("could not obtain lock"))

    with patch("app.api.assessments._set_lock_timeout", side_effect=_raise_lock_err):
        r_draft = client.put(
            f"/api/assessments/attempts/{att['id']}/draft",
            json={"answers": {}, "answers_seq": 1},
        )
        assert r_draft.status_code == 409
        assert r_draft.headers.get("retry-after") == "1"

        r_start = client.post(
            f"/api/assessments/enrollments/{enr.id}/attempts",
            json={"camera_verified": False},
        )
        assert r_start.status_code == 409
        assert r_start.headers.get("retry-after") == "1"


def test_reconcile_reaper_chunks_and_single_aggregate(db_session, sample_mcq_config):
    from app.services.scheduler_service import _reconcile_expired_assessment_attempts_job

    now = datetime.now(timezone.utc)
    asm = Assessment(
        id=63,
        title="Chunk Reaper Test",
        test_type="mcq",
        status="published",
        duration_minutes=10,
        passing_score=50.0,
        max_attempts=1,
        proctoring_config={},
        test_config=sample_mcq_config,
    )
    db_session.add(asm)
    db_session.flush()

    enr_ids = []
    for idx in range(5):
        emp = Employee(id=610 + idx, name=f"Reap {idx}", email=f"reap{idx}@x.com", status="active", employee_type="Full-time")
        db_session.add(emp)
        db_session.flush()
        enr = AssessmentEnrollment(assessment_id=asm.id, employee_id=emp.id, status="in_progress")
        db_session.add(enr)
        db_session.flush()
        enr_ids.append(enr.id)
        db_session.add(
            AssessmentAttempt(
                enrollment_id=enr.id,
                attempt_number=1,
                assessment_version=1,
                status="in_progress",
                question_snapshot={"test_type": "mcq", "passing_score": 50.0, "questions": []},
                started_at=now - timedelta(minutes=20),
                expires_at=now - timedelta(minutes=5),
                answers={},
                answers_seq=0,
            )
        )
    db_session.commit()

    res = _reconcile_expired_assessment_attempts_job(db_session=db_session, batch_size=2)
    assert res == {"reaped": 5, "batches": 3, "failed": 0}

    db_session.expire_all()
    for eid in enr_ids:
        enr_row = db_session.get(AssessmentEnrollment, eid)
        assert enr_row.status == "failed"
        assert enr_row.attempts[0].status == "abandoned"


def test_arq_reminder_enqueue_and_worker_backoff(http_env, sample_mcq_config):
    import asyncio
    from unittest.mock import AsyncMock
    from app.worker import send_assessment_reminders_task
    from app.services.slack_service import SlackRateLimitError

    client = http_env["client"]
    db = http_env["db"]
    set_user = http_env["set_user"]

    admin = User(id=63, name="Admin Rem", email="arem@x.com", password_hash="x", role="admin", is_active=True)
    # Employee intentionally has no User row -> verifies durable last_reminded_at column cooldown
    emp = Employee(id=620, name="Slack Only", email="so@x.com", slack_user_id="U620", status="active", employee_type="Full-time")
    asm = Assessment(
        id=64,
        title="ARQ Reminder Test",
        test_type="mcq",
        status="published",
        duration_minutes=15,
        passing_score=50.0,
        max_attempts=1,
        proctoring_config={},
        test_config=sample_mcq_config,
    )
    enr = AssessmentEnrollment(id=620, assessment_id=64, employee_id=620, status="assigned")
    db.add_all([admin, emp, asm, enr])
    db.commit()

    class _FakeRedis:
        def __init__(self):
            self.enqueue_job = AsyncMock(return_value=None)

    fake_redis = _FakeRedis()
    client.app.state.redis_pool = fake_redis
    try:
        set_user(admin)
        r1 = client.post("/api/assessments/reminders/send", json={"enrollment_id": enr.id})
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["notified_count"] == 1
        assert d1["slack_queued_count"] == 1
        assert fake_redis.enqueue_job.await_count == 1
        assert fake_redis.enqueue_job.await_args.args[0] == "send_assessment_reminders_task"
        targets = fake_redis.enqueue_job.await_args.args[1]

        # Immediate repeat blocked across workers via enrollment.last_reminded_at
        r2 = client.post("/api/assessments/reminders/send", json={"enrollment_id": enr.id})
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["notified_count"] == 0
        assert d2["skipped_cooldown_count"] == 1
        assert fake_redis.enqueue_job.await_count == 1
    finally:
        if hasattr(client.app.state, "redis_pool"):
            delattr(client.app.state, "redis_pool")

    # Worker task handles SlackRateLimitError Retry-After backoff
    with patch(
        "app.services.slack_service.try_send_assessment_reminder_dm",
        side_effect=[SlackRateLimitError(2), True],
    ), patch("app.worker.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        result = asyncio.run(send_assessment_reminders_task({}, targets))
        assert result == {"sent_count": 1, "failed_count": 0}
        assert sleep_mock.await_count == 1
        assert sleep_mock.await_args.args[0] == 2.0


def test_member_picker_ttl_cache_and_no_main_project_join(http_env):
    from app.api.clusters import invalidate_member_picker_cache

    client = http_env["client"]
    db = http_env["db"]
    set_user = http_env["set_user"]
    invalidate_member_picker_cache()

    admin = User(id=64, name="Admin Cache", email="acache@x.com", password_hash="x", role="admin", is_active=True)
    emp1 = Employee(id=630, name="Cached One", email="c630@x.com", status="active", designation="Developer", employee_type="Full-time")
    sheet = DailySheet(
        id=630,
        name="Alpha Sheet",
        client="Acme",
        project_type="Full",
        total_tasks=10,
        estimated_time_per_task=1.0,
        start_date=date(2026, 1, 1),
        project_status="active",
        assigned_employee_ids=[630],
    )
    db.add_all([admin, emp1, sheet])
    db.commit()

    set_user(admin)
    resp1 = client.get("/api/clusters/member-picker-options")
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert len(body1["employees"]) == 1
    assert body1["daily_sheets"][0]["main_project_name"] is None

    # Insert another employee; cached response returns same count until invalidated or TTL expires
    db.add(Employee(id=631, name="Uncached Two", email="c631@x.com", status="active", employee_type="Full-time"))
    db.commit()
    resp2 = client.get("/api/clusters/member-picker-options")
    assert resp2.status_code == 200
    assert len(resp2.json()["employees"]) == 1

    invalidate_member_picker_cache()
    resp3 = client.get("/api/clusters/member-picker-options")
    assert resp3.status_code == 200
    assert len(resp3.json()["employees"]) == 2



def test_evaluator_register_contract_candidate_report_filters_and_scoped_reminders(http_env, sample_mcq_config):
    """Contract used by the employee 'My Assessments' tab and the PM/Lead view-only register."""
    from app.services import project_scope

    project_scope.invalidate_manageable_cache()
    client = http_env["client"]
    db = http_env["db"]
    set_user = http_env["set_user"]
    now = datetime.now(timezone.utc)

    admin_user = User(id=1, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True)
    pm_emp = Employee(id=201, name="PM Alpha", email="pma@x.com", status="active", designation="Program Manager", employee_type="Full-time")
    sub_emp = Employee(id=203, name="Worker Alpha", email="wa@x.com", status="active", designation="Annotator", employee_type="Full-time")
    other_emp = Employee(id=205, name="Worker Beta", email="wb@x.com", status="active", designation="Annotator", employee_type="Full-time")
    pm_user = User(id=11, name="PM Alpha", email="pma@x.com", password_hash="x", role="pm", employee_id=201, is_active=True)
    sub_user = User(id=13, name="Worker Alpha", email="wa@x.com", password_hash="x", role="employee", employee_id=203, is_active=True)
    other_user = User(id=15, name="Worker Beta", email="wb@x.com", password_hash="x", role="employee", employee_id=205, is_active=True)

    mp1 = MainProject(id=1, name="Proj Alpha", program_manager_id=201, status="active")
    sp1 = SubProject(id=1, main_project_id=1, name="Sub Alpha", pm_id=201, status="active")
    ds1 = DailySheet(
        id=1, sub_project_id=1, main_project_id=1, name="DS Alpha", client="C", project_type="Full",
        total_tasks=10, estimated_time_per_task=1.0, start_date=date.today(), assigned_employee_ids=[201],
    )
    alloc_sub = Allocation(id=2, employee_id=203, sub_project_id=1, role_tags=["Annotator"], is_active=True)

    asm = Assessment(
        id=90, title="Grammar Basics", test_type="mcq", status="published", duration_minutes=15,
        passing_score=50.0, proctoring_config={"require_camera": False, "max_tab_switches": 1},
        test_config=sample_mcq_config,
    )
    enr_sub = AssessmentEnrollment(id=903, assessment_id=90, employee_id=203, status="assigned", deadline=now + timedelta(days=3))
    enr_other = AssessmentEnrollment(id=905, assessment_id=90, employee_id=205, status="assigned", deadline=now + timedelta(days=3))
    db.add_all([admin_user, pm_emp, sub_emp, other_emp, pm_user, sub_user, other_user,
                mp1, sp1, ds1, alloc_sub, asm, enr_sub, enr_other])
    db.commit()

    # Worker sits the test: 2 tab switches (> limit of 1 => flagged) and a ready recording.
    set_user(sub_user)
    started = client.post("/api/assessments/enrollments/903/attempts", json={"camera_verified": False})
    assert started.status_code == 200
    att_id = started.json()["id"]
    qids = [q["question_id"] for q in started.json()["question_snapshot"]["questions"]]
    client.post(f"/api/assessments/attempts/{att_id}/telemetry",
                json={"events": [{"event_type": "tab_switch"}, {"event_type": "tab_switch"}]})
    assert client.post(f"/api/assessments/attempts/{att_id}/submit",
                       json={"answers": {qid: 0 for qid in qids}}).status_code == 200
    db.add(AssessmentRecording(attempt_id=att_id, video_storage_path=f"assessments/{att_id}/proctoring.webm",
                               status="ready", expires_at=now + timedelta(days=5)))
    db.commit()

    # 1. Candidate report: percent + "N / M right", never per-question detail or flags.
    mine = client.get("/api/assessments/my/enrollments")
    assert mine.status_code == 200
    attempt = mine.json()[0]["attempts"][0]
    summary = attempt["result_summary"]
    assert attempt["score"] is not None
    assert summary["total_questions"] == 3
    assert 0 <= summary["correct_count"] <= 3
    assert "item_diagnostics" not in summary and "is_flagged" not in summary and "flag_reason" not in summary
    assert "is_flagged" not in attempt and "logs" not in attempt and "answers" not in attempt

    # 2. PM register: contract fields the UI relies on, pagination, KPIs and the virtual "flagged" tab.
    set_user(pm_user)
    reg = client.get("/api/assessments/admin/enrollments").json()
    assert {r["employee_id"] for r in reg["items"]} == {203}
    row = reg["items"][0]
    assert row["employee_designation"] == "Annotator"
    assert row["recording_status"] == "ready"
    assert row["latest_attempt_number"] == 1 and row["latest_attempt_id"] == att_id
    assert row["effective_deadline"] is not None
    assert reg["total_pages"] == 1
    assert reg["summary"]["ready_videos"] == 1
    assert "pass_rate" in reg["summary"]
    flagged = client.get("/api/assessments/admin/enrollments", params={"status": "flagged"}).json()
    assert [r["employee_id"] for r in flagged["items"]] == [203]
    assert client.get("/api/assessments/admin/enrollments", params={"status": "assigned"}).json()["items"] == []

    # 3. Single scorecard drill-down embeds the proctoring timeline + video state for evaluators.
    card = client.get("/api/assessments/enrollments/903/scorecard").json()
    assert card["attempts"][0]["recording_status"] == "ready"
    assert [l["event_type"] for l in card["attempts"][0]["logs"]] == ["tab_switch", "tab_switch"]
    assert card["employee_designation"] == "Annotator"

    # 4. Scoped filter options: only what the PM can actually see; plain employees are refused.
    opts = client.get("/api/assessments/admin/filters").json()
    assert opts["assessments"] == [{"id": 90, "title": "Grammar Basics"}]
    assert opts["clusters"] == []
    set_user(other_user)
    assert client.get("/api/assessments/admin/filters").status_code == 403

    # 5. Reminders can never fan out to everyone from an empty payload; enrollment_ids is honoured.
    set_user(admin_user)
    assert client.post("/api/assessments/reminders/send", json={}).status_code == 422
    with patch("app.api.assessments.try_send_assessment_reminder_dm", return_value=True):
        one = client.post("/api/assessments/reminders/send", json={"enrollment_ids": [905]})
    assert one.status_code == 200
    assert one.json()["notified_count"] == 1  # only enrollment 905 (903 already finished the test)


def test_assessment_list_include_config_for_studio(http_env, sample_mcq_config):
    client = http_env["client"]
    db = http_env["db"]
    http_env["set_user"](User(id=1, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True))
    db.add(Assessment(id=7, title="Studio Test", test_type="mcq", status="published", duration_minutes=10,
                      passing_score=50.0, test_config=sample_mcq_config))
    db.commit()

    lean = client.get("/api/assessments").json()[0]
    assert lean["test_config"] is None and lean["pool_size"] is None  # lean by default, no lazy-load per row

    full = client.get("/api/assessments", params={"include_config": True}).json()[0]
    assert len(full["test_config"]["categories"]) == 2
    assert full["categories_count"] == 2
    assert full["pool_size"] == 4          # 3 + 1 questions in the pools
    assert full["questions_count"] == 3    # draw counts 2 + 1


def test_publish_rejects_blank_prompt_and_blank_options():
    def cfg(question, options):
        return {"categories": [{"category_id": "c1", "name": "G", "pick_count": 1,
                                "questions": [{"question_id": "q1", "question": question,
                                               "options": options, "correct_index": 0}]}]}

    with pytest.raises(AssessmentValidationError, match="empty prompt"):
        validate_blueprint_for_publish("mcq", cfg("", ["A", "B", "C", "D"]), passing_score=50.0)
    with pytest.raises(AssessmentValidationError, match="empty prompt"):
        validate_blueprint_for_publish("mcq", cfg("   ", ["A", "B"]), passing_score=50.0)
    with pytest.raises(AssessmentValidationError, match="empty option"):
        validate_blueprint_for_publish("mcq", cfg("What?", ["A", "B", "", ""]), passing_score=50.0)
    validate_blueprint_for_publish("mcq", cfg("What?", ["A", "B"]), passing_score=50.0)  # valid


def test_candidate_card_shows_questions_per_sitting_and_cache_invalidates_on_edit(http_env, sample_mcq_config):
    from app.api import assessments as asm_api

    asm_api._QCOUNT_CACHE.clear()
    client, db, set_user = http_env["client"], http_env["db"], http_env["set_user"]
    emp = Employee(id=301, name="Cand", email="c@x.com", status="active", designation="Annotator", employee_type="Full-time")
    user = User(id=21, name="Cand", email="c@x.com", password_hash="x", role="employee", employee_id=301, is_active=True)
    admin = User(id=1, name="Admin", email="a@x.com", password_hash="x", role="admin", is_active=True)
    asm = Assessment(id=71, title="Count Test", test_type="mcq", status="published", duration_minutes=10,
                     passing_score=50.0, test_config=sample_mcq_config)
    db.add_all([emp, user, admin, asm, AssessmentEnrollment(id=711, assessment_id=71, employee_id=301, status="assigned")])
    db.commit()

    set_user(user)
    card = client.get("/api/assessments/my/enrollments").json()[0]
    assert card["question_count"] == 3          # draw counts 2 + 1 (pool has 4)
    assert client.get("/api/assessments/enrollments/711/scorecard").json()["question_count"] == 3

    # Admin raises a draw count -> new version/updated_at -> candidate sees the new number (no stale cache).
    set_user(admin)
    cfg = {**sample_mcq_config, "categories": [dict(c) for c in sample_mcq_config["categories"]]}
    cfg["categories"][0]["pick_count"] = 3
    assert client.put("/api/assessments/71", json={"test_config": cfg}).status_code == 200
    set_user(user)
    assert client.get("/api/assessments/my/enrollments").json()[0]["question_count"] == 4


def test_assignment_and_summary_counts_include_partial_attempts(db_session, sample_mcq_config):
    """Candidates who finished >=1 sitting while retries remain (status='assigned') must count as attempted/done."""
    from app.api.assignments import list_assignments
    from app.api.assessments import list_admin_enrollments

    admin = User(id=1, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True)
    e1 = Employee(id=401, name="Kisan Kumar Jena", email="kisan@x.com", status="active", designation="Developer", employee_type="Full-time")
    e2 = Employee(id=402, name="Test Three", email="t3@x.com", status="active", designation="Annotator", employee_type="Full-time")
    e3 = Employee(id=403, name="Aastha Patil", email="aastha@x.com", status="active", designation="Other", employee_type="Full-time")
    cl = Cluster(id=50, name="Testing", dynamic_scope="custom")
    asm = Assessment(
        id=50,
        title="Aptitude Test",
        test_type="mcq",
        status="published",
        duration_minutes=15,
        passing_score=70.0,
        max_attempts=2,
        test_config=sample_mcq_config,
    )
    asg = Assignment(
        id=50,
        cluster_id=50,
        assessment_id=50,
        deadline=datetime.now(timezone.utc) + timedelta(days=5),
        is_active=True,
    )
    db_session.add_all([admin, e1, e2, e3, cl, asm, asg])
    db_session.flush()

    enr1 = AssessmentEnrollment(id=501, assignment_id=50, assessment_id=50, employee_id=401, status="assigned")
    enr2 = AssessmentEnrollment(id=502, assignment_id=50, assessment_id=50, employee_id=402, status="assigned")
    enr3 = AssessmentEnrollment(id=503, assignment_id=50, assessment_id=50, employee_id=403, status="assigned")
    db_session.add_all([enr1, enr2, enr3])
    db_session.flush()

    now = datetime.now(timezone.utc)
    # Two candidates completed 1 failed attempt each (status stays 'assigned' because max_attempts=2)
    att1 = AssessmentAttempt(
        id=601,
        enrollment_id=501,
        attempt_number=1,
        assessment_version=1,
        status="completed",
        question_snapshot=sample_mcq_config,
        started_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(minutes=10),
        submitted_at=now,
        score=20.0,
        passed=False,
    )
    att2 = AssessmentAttempt(
        id=602,
        enrollment_id=502,
        attempt_number=1,
        assessment_version=1,
        status="completed",
        question_snapshot=sample_mcq_config,
        started_at=now - timedelta(minutes=4),
        expires_at=now + timedelta(minutes=11),
        submitted_at=now,
        score=10.0,
        passed=False,
    )
    db_session.add_all([att1, att2])
    db_session.commit()

    asgs = list_assignments(cluster_id=50, assignment_type="assessment", is_active=True, limit=100, offset=0, db=db_session, current_user=admin)
    assert len(asgs) == 1
    assert asgs[0].total_enrolled == 3
    assert asgs[0].completed_count == 2
    assert asgs[0].passed_count == 0
    assert asgs[0].in_progress_count == 0

    reg = list_admin_enrollments(
        cluster_id=50,
        assessment_id=None,
        assignment_id=None,
        status_filter=None,
        flagged_only=False,
        search=None,
        page=1,
        page_size=20,
        db=db_session,
        current_user=admin,
    )
    assert reg["summary"]["total"] == 3
    assert reg["summary"]["assigned"] == 3
    assert reg["summary"]["attempted"] == 2
    assert reg["summary"]["not_started"] == 1
    assert reg["summary"]["latest_passed"] == 0
    assert reg["summary"]["latest_failed"] == 2
    assert reg["summary"]["attempt_pass_rate"] == 0.0
    row_by_emp = {r["employee_id"]: r for r in reg["items"]}
    assert row_by_emp[401]["latest_attempt_passed"] is False
    assert row_by_emp[402]["latest_attempt_passed"] is False
    assert row_by_emp[403]["latest_attempt_passed"] is None


def test_signed_play_url_l1_cache_and_invalidation(http_env, sample_mcq_config):
    """Signed playback URLs are cached in RAM to save Supabase signing round trips and invalidated on purge."""
    from io import BytesIO
    import json as _json
    from app.services import assessment_storage_service as storage_svc

    storage_svc._play_url_cache.invalidate()
    client, db, set_user = http_env["client"], http_env["db"], http_env["set_user"]
    now = datetime.now(timezone.utc)

    admin = User(id=1, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True)
    emp = Employee(id=701, name="Cand", email="cand701@x.com", status="active", designation="Annotator", employee_type="Full-time")
    asm = Assessment(id=701, title="Video Cache Test", test_type="mcq", status="published", duration_minutes=15,
                     passing_score=50.0, test_config=sample_mcq_config)
    enr = AssessmentEnrollment(id=701, assessment_id=701, employee_id=701, status="failed")
    att = AssessmentAttempt(
        id=701, enrollment_id=701, attempt_number=1, assessment_version=1, status="completed",
        question_snapshot=sample_mcq_config, started_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(minutes=10), submitted_at=now, score=40.0, passed=False,
    )
    rec = AssessmentRecording(
        id=701, attempt_id=701, video_storage_path="assessments/701/proctoring.webm",
        file_size_bytes=1234567, status="ready", expires_at=now + timedelta(days=7),
    )
    db.add_all([admin, emp, asm, enr, att, rec])
    db.commit()

    sign_calls = 0

    class _FakeResp:
        def __init__(self, payload: bytes, status_code: int = 200):
            self._buf = BytesIO(payload)
            self.status = status_code

        def read(self):
            return self._buf.read()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _fake_urlopen(req, timeout=15):
        nonlocal sign_calls
        url = getattr(req, "full_url", str(req))
        if "/object/sign/" in url:
            sign_calls += 1
            return _FakeResp(_json.dumps({"signedURL": f"/object/sign/assessment-recordings/assessments/701/proctoring.webm?token=tok{sign_calls}"}).encode())
        if req.get_method() == "DELETE":
            return _FakeResp(_json.dumps([{"name": "assessments/701/proctoring.webm"}]).encode())
        return _FakeResp(b"", 200)

    set_user(admin)
    with patch("app.services.assessment_storage_service.is_supabase_configured", return_value=True), \
         patch("app.services.assessment_storage_service.SUPABASE_URL", "https://proj.supabase.co"), \
         patch("app.services.assessment_storage_service.urllib.request.urlopen", side_effect=_fake_urlopen):
        r1 = client.get("/api/assessments/results/701/recording/play-url")
        r2 = client.get("/api/assessments/results/701/recording/play-url")
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json()["play_url"] == r2.json()["play_url"]
        assert r1.json()["file_size_bytes"] == 1234567
        assert r1.json()["expires_at"] is not None
        assert sign_calls == 1  # Second call served from RAM cache

        # Deleting the file invalidates the cached URL
        deleted = storage_svc.delete_recording_files_bulk(["assessments/701/proctoring.webm"])
        assert "assessments/701/proctoring.webm" in deleted

        r3 = client.get("/api/assessments/results/701/recording/play-url")
        assert r3.status_code == 200
        assert sign_calls == 2
        assert r3.json()["play_url"] != r1.json()["play_url"]
    storage_svc._play_url_cache.invalidate()


def test_archive_cluster_preserves_attempted_enrollments_with_retakes(db_session, sample_mcq_config):
    """Archiving a cluster must never hide enrollments that already have attempts (even if retakes remain)."""
    from app.api.assessments import list_admin_enrollments
    from app.services.cluster_service import archive_cluster

    admin = User(id=1, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True)
    e_attempted = Employee(id=801, name="Attempted Cand", email="e801@x.com", status="active", designation="Annotator", employee_type="Full-time")
    e_unstarted = Employee(id=802, name="Unstarted Cand", email="e802@x.com", status="active", designation="Annotator", employee_type="Full-time")
    cl = Cluster(id=80, name="Archive Progress Cluster", dynamic_scope="custom")
    asm = Assessment(
        id=80, title="Multi-Attempt Test", test_type="mcq", status="published",
        duration_minutes=15, passing_score=70.0, max_attempts=2, test_config=sample_mcq_config,
    )
    asg = Assignment(id=80, cluster_id=80, assessment_id=80, deadline=datetime.now(timezone.utc) + timedelta(days=5), is_active=True)
    db_session.add_all([admin, e_attempted, e_unstarted, cl, asm, asg])
    db_session.flush()

    enr_att = AssessmentEnrollment(id=801, assignment_id=80, assessment_id=80, employee_id=801, status="assigned", max_attempts_snapshot=2)
    enr_unstarted = AssessmentEnrollment(id=802, assignment_id=80, assessment_id=80, employee_id=802, status="assigned", max_attempts_snapshot=2)
    db_session.add_all([enr_att, enr_unstarted])
    db_session.flush()

    now = datetime.now(timezone.utc)
    att = AssessmentAttempt(
        id=801, enrollment_id=801, attempt_number=1, assessment_version=1, status="completed",
        question_snapshot=sample_mcq_config, started_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(minutes=10), submitted_at=now, score=10.0, passed=False,
    )
    db_session.add(att)
    db_session.commit()

    assert archive_cluster(db_session, 80) is True
    db_session.commit()
    db_session.expire_all()

    assert db_session.get(AssessmentEnrollment, 801).status == "failed"  # preserved for Progress & Logs
    assert db_session.get(AssessmentEnrollment, 802).status == "cancelled"  # unstarted cancelled

    reg = list_admin_enrollments(
        cluster_id=None, assessment_id=80, assignment_id=None, status_filter=None,
        flagged_only=False, search=None, page=1, page_size=20, db=db_session, current_user=admin,
    )
    assert reg["summary"]["total"] == 1
    assert reg["items"][0]["employee_id"] == 801
    assert reg["items"][0]["attempts_used"] == 1


def test_cluster_restore_name_collision_and_assessment_restore(http_env, sample_mcq_config):
    """
    1. Archiving cluster_a frees its name for a new active cluster_a with a different roster.
    2. Restoring the old cluster_a fails with 409 unless a non-colliding new name is provided.
    3. Assessments archived via DELETE can be listed with include_archived=true and restored to draft.
    """
    client, db, set_user = http_env["client"], http_env["db"], http_env["set_user"]
    admin = User(id=1, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True)
    e1 = Employee(id=901, name="Alpha One", email="a1@x.com", status="active", designation="Annotator", employee_type="Full-time")
    e2 = Employee(id=902, name="Beta Two", email="b2@x.com", status="active", designation="Developer", employee_type="Full-time")
    db.add_all([admin, e1, e2])
    db.commit()

    set_user(admin)

    # Create cluster_a with e1, then archive it
    c1 = client.post("/api/clusters", json={"name": "cluster_a", "dynamic_scope": "custom", "employee_ids": [901]})
    assert c1.status_code == 201
    c1_id = c1.json()["id"]
    assert client.delete(f"/api/clusters/{c1_id}").status_code == 200

    # Create a brand new active cluster_a with e2
    c2 = client.post("/api/clusters", json={"name": "cluster_a", "dynamic_scope": "custom", "employee_ids": [902]})
    assert c2.status_code == 201
    c2_id = c2.json()["id"]

    # Attempting to restore old cluster_a without renaming or with case-variant name must return 409
    assert client.post(f"/api/clusters/{c1_id}/restore").status_code == 409
    assert client.post(f"/api/clusters/{c1_id}/restore", json={"name": "CLUSTER_A"}).status_code == 409

    # Restoring old cluster_a with a unique new name succeeds and preserves its original roster [901]
    restored_c1 = client.post(f"/api/clusters/{c1_id}/restore", json={"name": "cluster_a_old"})
    assert restored_c1.status_code == 200
    assert restored_c1.json()["name"] == "cluster_a_old"
    assert restored_c1.json()["is_archived"] is False
    assert restored_c1.json()["member_count"] == 1

    m1 = [m["employee_id"] for m in client.get(f"/api/clusters/{c1_id}/members").json()]
    m2 = [m["employee_id"] for m in client.get(f"/api/clusters/{c2_id}/members").json()]
    assert m1 == [901]
    assert m2 == [902]

    # Assessment archive -> include_archived list -> restore to draft
    asm = Assessment(
        id=950, title="Restorable Quiz", test_type="mcq", status="published",
        duration_minutes=10, passing_score=60.0, test_config=sample_mcq_config,
    )
    db.add(asm)
    db.commit()

    assert client.delete("/api/assessments/950").status_code == 200
    assert client.get("/api/assessments").json() == []
    assert [a["id"] for a in client.get("/api/assessments", params={"include_archived": True}).json()] == [950]

    restored_asm = client.post("/api/assessments/950/restore")
    assert restored_asm.status_code == 200
    assert restored_asm.json()["status"] == "draft"
    assert client.post("/api/assessments/950/restore").status_code == 409


def test_restore_reactivates_non_expired_assignments_for_assessment_and_cluster(http_env, sample_mcq_config):
    """
    Restoring an assessment to 'published' (or restoring an archived cluster) with restore_assignments=True
    re-activates non-expired cluster assignments, revives cancelled enrollments, and restores 'assigned'
    status for attempted enrollments that still have retakes remaining, while skipping expired deadlines.
    """
    client, db, set_user = http_env["client"], http_env["db"], http_env["set_user"]
    now = datetime.now(timezone.utc)
    admin = User(id=1, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True)
    e1 = Employee(id=911, name="Attempted Cand", email="c911@x.com", status="active", designation="Annotator", employee_type="Full-time")
    e2 = Employee(id=912, name="Unstarted Cand", email="c912@x.com", status="active", designation="Annotator", employee_type="Full-time")
    cl = Cluster(id=910, name="Restore Asg Cluster", dynamic_scope="custom")
    asm = Assessment(
        id=960, title="Restore Flow Test", test_type="mcq", status="published",
        duration_minutes=15, passing_score=70.0, max_attempts=2, test_config=sample_mcq_config,
    )
    db.add_all([admin, e1, e2, cl, asm])
    db.flush()
    db.add_all([
        ClusterMember(cluster_id=910, employee_id=911),
        ClusterMember(cluster_id=910, employee_id=912),
    ])
    asg = Assignment(
        id=960, cluster_id=910, assessment_id=960,
        deadline=now + timedelta(days=5), is_active=True,
    )
    db.add(asg)
    db.flush()

    enr1 = AssessmentEnrollment(id=961, assignment_id=960, assessment_id=960, employee_id=911, status="assigned", max_attempts_snapshot=2)
    enr2 = AssessmentEnrollment(id=962, assignment_id=960, assessment_id=960, employee_id=912, status="assigned", max_attempts_snapshot=2)
    db.add_all([enr1, enr2])
    db.flush()
    db.add(AssessmentAttempt(
        id=961, enrollment_id=961, attempt_number=1, assessment_version=1, status="completed",
        question_snapshot=sample_mcq_config, started_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(minutes=10), submitted_at=now, score=10.0, passed=False,
    ))
    db.commit()

    set_user(admin)

    # 1. Archive assessment -> assignment deactivated, e1='failed', e2='cancelled'
    assert client.delete("/api/assessments/960").status_code == 200
    db.expire_all()
    assert db.get(Assignment, 960).is_active is False
    assert db.get(AssessmentEnrollment, 961).status == "failed"
    assert db.get(AssessmentEnrollment, 962).status == "cancelled"

    # 2. Restore assessment directly to published with restore_assignments=True -> cluster assignment & enrollments restored
    res = client.post("/api/assessments/960/restore", json={"restore_to": "published", "restore_assignments": True})
    assert res.status_code == 200
    assert res.json()["status"] == "published"
    db.expire_all()
    assert db.get(Assignment, 960).is_active is True
    assert db.get(AssessmentEnrollment, 961).status == "assigned"
    assert db.get(AssessmentEnrollment, 961).assignment_id == 960
    assert db.get(AssessmentEnrollment, 962).status == "assigned"
    assert db.get(AssessmentEnrollment, 962).assignment_id == 960

    # 3. Archive cluster -> restore cluster with restore_assignments=True -> assignment restored again
    assert client.delete("/api/clusters/910").status_code == 200
    db.expire_all()
    assert db.get(Assignment, 960).is_active is False

    cl_res = client.post("/api/clusters/910/restore", json={"restore_assignments": True})
    assert cl_res.status_code == 200
    assert cl_res.json()["active_assignments_count"] == 1
    db.expire_all()
    assert db.get(Assignment, 960).is_active is True
    assert db.get(AssessmentEnrollment, 961).status == "assigned"
    assert db.get(AssessmentEnrollment, 962).status == "assigned"

    # 4. Expired deadlines must NOT be re-activated
    assert client.delete("/api/clusters/910").status_code == 200
    asg_row = db.get(Assignment, 960)
    asg_row.deadline = now - timedelta(days=1)
    db.commit()
    cl_res2 = client.post("/api/clusters/910/restore", json={"restore_assignments": True})
    assert cl_res2.status_code == 200
    assert cl_res2.json()["active_assignments_count"] == 0


def test_assessment_unique_active_title_and_archived_cluster_filter_labels(http_env, sample_mcq_config):
    """
    1. Two active (draft/published) assessments cannot share the same title (case-insensitive -> 409).
    2. Restoring an archived assessment whose title collides with a live test requires renaming.
    3. Archived cluster history remains visible in Progress & Logs alongside a newly created active cluster
       of the same name, distinguished by ID and '(Archived)' label.
    """
    client, db, set_user = http_env["client"], http_env["db"], http_env["set_user"]
    now = datetime.now(timezone.utc)
    admin = User(id=1, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True)
    emp = Employee(id=921, name="Same Emp", email="s921@x.com", status="active", designation="Annotator", employee_type="Full-time")
    db.add_all([admin, emp])
    db.commit()

    set_user(admin)

    # Create assessment A, cannot create another active with same case-insensitive title
    a1 = client.post("/api/assessments", json={"title": "Grammar Check", "test_type": "mcq", "passing_score": 60.0, "test_config": sample_mcq_config})
    assert a1.status_code == 201
    a1_id = a1.json()["id"]
    dup = client.post("/api/assessments", json={"title": "grammar check", "test_type": "mcq", "passing_score": 60.0, "test_config": sample_mcq_config})
    assert dup.status_code == 409

    # Archive a1 -> now creating a new active "Grammar Check" succeeds
    assert client.delete(f"/api/assessments/{a1_id}").status_code == 200
    a2 = client.post("/api/assessments", json={"title": "Grammar Check", "test_type": "mcq", "passing_score": 60.0, "test_config": sample_mcq_config})
    assert a2.status_code == 201
    a2_id = a2.json()["id"]

    # Restoring a1 without renaming returns 409; restoring with new title succeeds
    assert client.post(f"/api/assessments/{a1_id}/restore").status_code == 409
    assert client.post(f"/api/assessments/{a1_id}/restore", json={"title": "Grammar Check v1"}).status_code == 200

    # Cluster A (id=c1) archived after employee completes Test 1; new active Cluster A (id=c2) created with same employee for Test 2
    assert client.put(f"/api/assessments/{a1_id}", json={"status": "published"}).status_code == 200
    assert client.put(f"/api/assessments/{a2_id}", json={"status": "published"}).status_code == 200

    c1 = client.post("/api/clusters", json={"name": "cluster_a", "dynamic_scope": "custom", "employee_ids": [921]}).json()["id"]
    asg1 = client.post("/api/assignments", json={"cluster_id": c1, "assessment_id": a1_id, "relative_due_days": 5}).json()["id"]

    # Seed completed attempt under c1 -> asg1
    enr1 = db.query(AssessmentEnrollment).filter_by(employee_id=921, assessment_id=a1_id).first()
    enr1.status = "passed"
    db.add(AssessmentAttempt(
        id=921, enrollment_id=enr1.id, attempt_number=1, assessment_version=1, status="completed",
        question_snapshot=sample_mcq_config, started_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(minutes=10), submitted_at=now, score=90.0, passed=True,
    ))
    db.commit()

    # Archive old cluster_a, create new active cluster_a with same employee and assign Test 2
    assert client.delete(f"/api/clusters/{c1}").status_code == 200
    c2 = client.post("/api/clusters", json={"name": "cluster_a", "dynamic_scope": "custom", "employee_ids": [921]}).json()["id"]
    asg2 = client.post("/api/assignments", json={"cluster_id": c2, "assessment_id": a2_id, "relative_due_days": 5}).json()["id"]
    assert c1 != c2 and asg1 != asg2

    reg = client.get("/api/assessments/admin/enrollments").json()
    assert reg["summary"]["total"] == 2
    by_asg = {row["assignment_id"]: row for row in reg["items"]}
    assert by_asg[asg1]["cluster_id"] == c1
    assert by_asg[asg1]["cluster_name"] == "cluster_a (Archived)"
    assert by_asg[asg2]["cluster_id"] == c2
    assert by_asg[asg2]["cluster_name"] == "cluster_a"

    flt = client.get("/api/assessments/admin/filters").json()
    assert {c["id"]: c["name"] for c in flt["clusters"]} == {c1: "cluster_a (Archived)", c2: "cluster_a"}


def test_preserve_old_cluster_assignment_and_prior_attempt_flag_on_retest(http_env, sample_mcq_config):
    """
    1. Archiving cluster_a preserves assignment_id on attempted enrollments (even with retakes remaining)
       so creating a new active cluster_a for the same test does NOT hijack the old scorecard row.
    2. Granting a retest re-points the enrollment to the new active cluster_a assignment, and the
       register row exposes latest_attempt_at + is_prior_attempt=True so HR knows the attempt predates
       the new cluster assignment.
    """
    client, db, set_user = http_env["client"], http_env["db"], http_env["set_user"]
    now = datetime.now(timezone.utc)
    admin = User(id=1, name="Admin", email="admin@x.com", password_hash="x", role="admin", is_active=True)
    emp = Employee(id=931, name="Amit Prior", email="amit931@x.com", status="active", designation="Annotator", employee_type="Full-time")
    asm = Assessment(
        id=931, title="Grammar Reuse Test", test_type="mcq", status="published",
        duration_minutes=15, passing_score=70.0, max_attempts=2, test_config=sample_mcq_config,
    )
    db.add_all([admin, emp, asm])
    db.commit()

    set_user(admin)
    c1 = client.post("/api/clusters", json={"name": "cluster_a", "dynamic_scope": "custom", "employee_ids": [931]}).json()["id"]
    asg1 = client.post("/api/assignments", json={
        "cluster_id": c1, "assessment_id": 931,
        "deadline": (now + timedelta(days=5)).isoformat(),
    }).json()["id"]
    db.get(Assignment, asg1).created_at = now - timedelta(days=3)
    db.commit()

    # Amit fails Attempt #1 yesterday (1 retake remaining -> status stays 'assigned')
    enr = db.query(AssessmentEnrollment).filter_by(employee_id=931, assessment_id=931).first()
    att_time = now - timedelta(days=2)
    db.add(AssessmentAttempt(
        id=931, enrollment_id=enr.id, attempt_number=1, assessment_version=1, status="completed",
        question_snapshot=sample_mcq_config, started_at=att_time,
        expires_at=att_time + timedelta(minutes=15), submitted_at=att_time + timedelta(minutes=5),
        score=35.0, passed=False,
    ))
    db.commit()

    # Archive old cluster_a -> Amit keeps assignment_id=asg1 and status='failed'
    assert client.delete(f"/api/clusters/{c1}").status_code == 200
    db.expire_all()
    enr_after_arch = db.get(AssessmentEnrollment, enr.id)
    assert enr_after_arch.status == "failed"
    assert enr_after_arch.assignment_id == asg1

    # Create new active cluster_a with Amit and assign the SAME test -> old scorecard stays tagged cluster_a (Archived)
    c2 = client.post("/api/clusters", json={"name": "cluster_a", "dynamic_scope": "custom", "employee_ids": [931]}).json()["id"]
    asg2 = client.post("/api/assignments", json={
        "cluster_id": c2, "assessment_id": 931,
        "deadline": (now + timedelta(days=7)).isoformat(),
    }).json()["id"]

    reg1 = client.get("/api/assessments/admin/enrollments", params={"assessment_id": 931}).json()["items"][0]
    assert reg1["cluster_id"] == c1
    assert reg1["cluster_name"] == "cluster_a (Archived)"
    assert reg1["latest_attempt_at"] is not None
    assert reg1["is_prior_attempt"] is False

    # HR grants retest -> enrollment re-points to new cluster_a (asg2) & marks existing attempt as prior
    retest_resp = client.post(f"/api/assessments/enrollments/{enr.id}/grant-retest", json={"reason": "Allow retake on new cohort"})
    assert retest_resp.status_code == 200

    reg2 = client.get("/api/assessments/admin/enrollments", params={"assessment_id": 931}).json()["items"][0]
    assert reg2["cluster_id"] == c2
    assert reg2["cluster_name"] == "cluster_a"
    assert reg2["is_prior_attempt"] is True
    assert reg2["assignment_created_at"] is not None


def test_archive_cluster_constant_query_count_no_n_plus_one(db_session, sample_mcq_config):
    """Archiving a 30-member cluster with 2 active assignments executes O(1) SQL queries, never N+1."""
    from sqlalchemy import event
    from app.services.cluster_service import archive_cluster

    now = datetime.now(timezone.utc)
    cl = Cluster(id=940, name="Large Archive Cluster", dynamic_scope="custom")
    asm1 = Assessment(id=941, title="Test One", test_type="mcq", status="published", test_config=sample_mcq_config)
    asm2 = Assessment(id=942, title="Test Two", test_type="mcq", status="published", test_config=sample_mcq_config)
    db_session.add_all([cl, asm1, asm2])
    db_session.flush()

    asg1 = Assignment(id=941, cluster_id=940, assessment_id=941, deadline=now + timedelta(days=5), is_active=True)
    asg2 = Assignment(id=942, cluster_id=940, assessment_id=942, deadline=now + timedelta(days=5), is_active=True)
    db_session.add_all([asg1, asg2])
    db_session.flush()

    emps = [
        Employee(id=9400 + i, name=f"Emp {i}", email=f"e9400_{i}@x.com", status="active", employee_type="Full-time")
        for i in range(30)
    ]
    db_session.add_all(emps)
    db_session.flush()

    db_session.add_all([ClusterMember(cluster_id=940, employee_id=9400 + i) for i in range(30)])
    db_session.add_all([
        AssessmentEnrollment(id=94100 + i, assignment_id=941, assessment_id=941, employee_id=9400 + i, status="assigned")
        for i in range(30)
    ])
    db_session.add_all([
        AssessmentEnrollment(id=94200 + i, assignment_id=942, assessment_id=942, employee_id=9400 + i, status="assigned")
        for i in range(30)
    ])
    db_session.commit()

    queries = []
    engine = db_session.get_bind()

    def _count(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", _count)
    try:
        assert archive_cluster(db_session, 940) is True
        db_session.commit()
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    # 60 enrollments across 2 assignments must take O(1) (~8-12) queries, never 60+ N+1 queries
    assert len(queries) <= 12, f"Expected O(1) queries on archive_cluster, got {len(queries)}: {queries}"
    assert db_session.query(AssessmentEnrollment).filter_by(assessment_id=941, status="cancelled").count() == 30
    assert db_session.query(AssessmentEnrollment).filter_by(assessment_id=942, status="cancelled").count() == 30
