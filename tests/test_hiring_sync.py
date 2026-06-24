"""
Hiring sync tests
=================
Automated pytest suite for the hiring sync pipeline.
No real database, no real email, no real hiring portal needed —
everything external is mocked so tests run fast and offline.

New flow (as of hiring sync v2):
  - run_sync() creates SignupRequest (pending) — NOT Employee/User directly
  - Admin approves signup request → existing approval logic creates Employee+User+email
  - Email is sent by signup_requests.py approve endpoint, NOT by run_sync

Tests cover:
  - New candidate creates a SignupRequest row (pending)
  - Duplicate detection via Employee table
  - Duplicate detection via User table (orphaned row)
  - Duplicate detection via SignupRequest table (pending/approved)
  - Rejected signup request is replaced on re-import
  - Candidate with hr_status != "hired" is silently ignored
  - Candidate missing name or email lands in errors[], not a crash
  - job_type / job_title mapping stored correctly in SignupRequest
  - /preview endpoint reports would_import correctly
  - /sync endpoint returns correct summary counts
  - Both endpoints return 502 when hiring portal is unreachable

Run:
    venv/Scripts/python -m pytest tests/test_hiring_sync.py -v
"""
import os
os.environ.pop("SLACK_BOT_TOKEN", None)

import sys
import pytest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.db.database as database
from app.db.database import Base

# Import all models so SQLAlchemy knows about every table before create_all()
import app.models.admin            # noqa: F401
import app.models.allocation       # noqa: F401
import app.models.employee         # noqa: F401
import app.models.guideline        # noqa: F401
import app.models.leave            # noqa: F401
import app.models.notification     # noqa: F401
import app.models.onboarding       # noqa: F401
import app.models.parent_project   # noqa: F401
import app.models.payroll          # noqa: F401
import app.models.performance_review  # noqa: F401
import app.models.product_manager  # noqa: F401
import app.models.project          # noqa: F401
import app.models.referral         # noqa: F401
import app.models.side_project     # noqa: F401
import app.models.signup_request   # noqa: F401
import app.models.skill            # noqa: F401
import app.models.sub_project      # noqa: F401
import app.models.user             # noqa: F401
import app.models.wfh              # noqa: F401

from app.models.employee import Employee
from app.models.signup_request import SignupRequest
from app.models.user import User
from app.api.hiring_sync import router as hiring_sync_router
from app.services.hiring_sync_service import run_sync


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def client_and_db():
    """Fixture for API-level tests — returns (TestClient, db_session)."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    test_app = FastAPI()
    test_app.include_router(hiring_sync_router)
    test_app.dependency_overrides[database.get_db] = override_get_db

    db = TestingSessionLocal()
    yield TestClient(test_app), db
    db.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_only():
    """Fixture for unit-level tests that call run_sync() directly."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSessionLocal()
    yield db
    db.close()
    Base.metadata.drop_all(bind=engine)


# ── Helper: build a fake candidate dict ──────────────────────────────────────

def _candidate(
    name="Alice Smith",
    email="alice@example.com",
    job_title="Developer",
    job_type="full-time",
    hr_status="hired",
    application_id="APP-001",
):
    return {
        "name": name,
        "email": email,
        "job_title": job_title,
        "job_type": job_type,
        "hr_status": hr_status,
        "application_id": application_id,
        "doc_status": "complete",
        "phone": "9999999999",
    }


# ── Unit tests: run_sync() ────────────────────────────────────────────────────

def test_new_candidate_creates_signup_request(db_only):
    # Happy path: a hired candidate creates a pending SignupRequest — NOT Employee/User
    candidates = [_candidate()]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates):
        result = run_sync(db_only)

    assert result["imported"] == 1
    assert result["skipped"] == 0
    assert result["errors"] == 0

    # SignupRequest created with correct data
    req = db_only.query(SignupRequest).filter(SignupRequest.email == "alice@example.com").first()
    assert req is not None
    assert req.name == "Alice Smith"
    assert req.status == "pending"
    assert req.employee_type == "Full-time"
    assert req.designation == "Developer"

    # Employee and User must NOT be created yet — only after admin approves
    assert db_only.query(Employee).filter(Employee.email == "alice@example.com").first() is None
    assert db_only.query(User).filter(User.email == "alice@example.com").first() is None


def test_hiring_portal_source_recorded_in_reason(db_only):
    # The reason field should record that this came from the hiring portal
    candidates = [_candidate(application_id="APP-999")]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates):
        run_sync(db_only)

    req = db_only.query(SignupRequest).filter(SignupRequest.email == "alice@example.com").first()
    assert "hiring portal" in req.reason.lower()
    assert "APP-999" in req.reason


def test_duplicate_via_employee_table_is_skipped(db_only):
    # If Employee already exists for the email, skip — don't create another signup request
    candidates = [_candidate()]

    existing_emp = Employee(
        name="Alice Smith", email="alice@example.com",
        employee_type="Full-time", status="active",
    )
    db_only.add(existing_emp)
    db_only.commit()

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates):
        result = run_sync(db_only)

    assert result["imported"] == 0
    assert result["skipped"] == 1
    assert db_only.query(SignupRequest).count() == 0


def test_duplicate_via_user_table_is_skipped(db_only):
    # If User already exists (orphaned — no Employee row), skip gracefully
    candidates = [_candidate()]

    existing_user = User(
        name="Alice Smith", email="alice@example.com",
        password_hash="irrelevant", role="employee", is_active=True,
    )
    db_only.add(existing_user)
    db_only.commit()

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates):
        result = run_sync(db_only)

    assert result["imported"] == 0
    assert result["skipped"] == 1


def test_duplicate_via_pending_signup_request_is_skipped(db_only):
    # If a pending SignupRequest already exists, don't create another one
    candidates = [_candidate()]

    existing_req = SignupRequest(
        name="Alice Smith", email="alice@example.com",
        employee_type="Full-time", status="pending",
    )
    db_only.add(existing_req)
    db_only.commit()

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates):
        result = run_sync(db_only)

    assert result["imported"] == 0
    assert result["skipped"] == 1
    assert db_only.query(SignupRequest).count() == 1  # still only the original


def test_rejected_signup_request_is_replaced(db_only):
    # A previously rejected request should be replaced on re-import (allow second chance)
    candidates = [_candidate()]

    rejected_req = SignupRequest(
        name="Alice Smith", email="alice@example.com",
        employee_type="Full-time", status="rejected",
    )
    db_only.add(rejected_req)
    db_only.commit()

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates):
        result = run_sync(db_only)

    assert result["imported"] == 1
    new_req = db_only.query(SignupRequest).filter(SignupRequest.email == "alice@example.com").first()
    assert new_req.status == "pending"


def test_non_hired_candidate_is_ignored(db_only):
    # Only hr_status == "hired" triggers import — interviewing/screening are ignored
    candidates = [_candidate(hr_status="interviewing")]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates):
        result = run_sync(db_only)

    assert result["imported"] == 0
    assert result["skipped"] == 0
    assert result["errors"] == 0
    assert db_only.query(SignupRequest).count() == 0


def test_missing_email_recorded_as_error(db_only):
    # Bad data from hiring portal — empty email must not crash the sync
    candidates = [_candidate(email="")]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates):
        result = run_sync(db_only)

    assert result["errors"] == 1
    assert result["details"]["errors"][0]["reason"] == "Missing name or email"


def test_missing_name_recorded_as_error(db_only):
    candidates = [_candidate(name="")]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates):
        result = run_sync(db_only)

    assert result["errors"] == 1


def test_job_type_mapping(db_only):
    # "internship" → employee_type "Intern", "Reviewer" → designation "Reviewer"
    candidates = [_candidate(job_type="internship", job_title="Reviewer")]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates):
        run_sync(db_only)

    req = db_only.query(SignupRequest).filter(SignupRequest.email == "alice@example.com").first()
    assert req.employee_type == "Intern"
    assert req.designation == "Reviewer"


def test_unknown_job_title_defaults_to_annotator(db_only):
    # Unknown job title falls back to "Annotator"
    candidates = [_candidate(job_title="Wizard")]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates):
        run_sync(db_only)

    req = db_only.query(SignupRequest).filter(SignupRequest.email == "alice@example.com").first()
    assert req.designation == "Annotator"


def test_multiple_candidates_mixed_result(db_only):
    candidates = [
        _candidate(name="Alice", email="alice@x.com", application_id="A1"),
        _candidate(name="Bob",   email="bob@x.com",   application_id="A2", hr_status="interviewing"),
        _candidate(name="",      email="bad@x.com",   application_id="A3"),
    ]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates):
        result = run_sync(db_only)

    assert result["imported"] == 1   # Alice only
    assert result["skipped"] == 0    # Bob ignored (not hired)
    assert result["errors"] == 1     # bad@x.com — missing name
    assert db_only.query(SignupRequest).count() == 1


# ── API tests: /preview and /sync endpoints ───────────────────────────────────

def test_preview_endpoint_shows_would_import(client_and_db):
    # Fresh DB — no existing records — preview should say would_import=True
    client, _ = client_and_db
    candidates = [_candidate()]

    with patch("app.api.hiring_sync.fetch_hired_candidates", return_value=candidates):
        r = client.get("/api/hiring/preview")

    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert data["candidates"][0]["would_import"] is True
    assert data["candidates"][0]["skip_reason"] is None


def test_preview_shows_skip_reason_for_existing_employee(client_and_db):
    # Candidate already in employees table — preview must report would_import=False
    client, db = client_and_db

    db.add(Employee(name="Alice Smith", email="alice@example.com", employee_type="Full-time", status="active"))
    db.commit()

    candidates = [_candidate()]
    with patch("app.api.hiring_sync.fetch_hired_candidates", return_value=candidates):
        r = client.get("/api/hiring/preview")

    assert r.status_code == 200
    assert r.json()["candidates"][0]["would_import"] is False
    assert r.json()["candidates"][0]["skip_reason"] == "Already exists in PM portal"


def test_sync_endpoint_returns_summary(client_and_db):
    client, _ = client_and_db
    candidates = [_candidate()]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates):
        r = client.post("/api/hiring/sync")

    assert r.status_code == 200
    body = r.json()
    assert body["imported"] == 1
    assert body["skipped"] == 0
    assert body["errors"] == 0


def test_sync_endpoint_returns_502_when_hiring_portal_unreachable(client_and_db):
    client, _ = client_and_db

    with patch(
        "app.services.hiring_sync_service.fetch_hired_candidates",
        side_effect=RuntimeError("Cannot reach hiring portal: Connection refused"),
    ):
        r = client.post("/api/hiring/sync")

    assert r.status_code == 502
    assert "Cannot reach hiring portal" in r.json()["detail"]


def test_preview_endpoint_returns_502_when_hiring_portal_unreachable(client_and_db):
    client, _ = client_and_db

    # preview calls fetch_hired_candidates at the API module import site
    with patch(
        "app.api.hiring_sync.fetch_hired_candidates",
        side_effect=RuntimeError("Cannot reach hiring portal: Connection refused"),
    ):
        r = client.get("/api/hiring/preview")

    assert r.status_code == 502
