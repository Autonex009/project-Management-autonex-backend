"""
Hiring sync tests
=================
Automated pytest suite for the hiring sync pipeline.
No real database, no real email, no real hiring portal needed —
everything external is mocked so tests run fast and offline.

Tests cover:
  - New candidate creates Employee + User rows and fires email
  - Duplicate detection via Employee table (normal case)
  - Duplicate detection via User table (orphaned row edge case)
  - Candidate with hr_status != "hired" is silently ignored
  - Candidate missing name or email lands in errors[], not a crash
  - job_type / job_title mapping to internal designation values
  - /preview endpoint reports would_import correctly before any DB writes
  - /sync endpoint returns correct summary counts
  - Both endpoints return 502 when hiring portal is unreachable

Run:
    venv/Scripts/python -m pytest tests/test_hiring_sync.py -v
"""
import os
# Remove SLACK_BOT_TOKEN so the Slack client doesn't try to connect during import
os.environ.pop("SLACK_BOT_TOKEN", None)

import sys
import pytest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Make sure the backend root is on the path when running from the tests/ folder
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
from app.models.user import User
from app.api.hiring_sync import router as hiring_sync_router
from app.services.hiring_sync_service import run_sync


# ── Fixtures ──────────────────────────────────────────────────────────────────
# Each test gets a completely fresh in-memory SQLite database.
# Tables are created before the test and dropped after, so tests never share state.

@pytest.fixture()
def client_and_db():
    """
    Fixture for API-level tests.
    Returns (TestClient, db_session) so a test can both call HTTP endpoints
    and query the database directly to verify what was stored.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Replace the real get_db dependency with one that uses our test DB
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
    """
    Fixture for unit-level tests that call run_sync() directly.
    Only a db session is needed — no HTTP client.
    """
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
# Mirrors the shape returned by the hiring portal API.
# Default values represent a normal hired developer — override only what a test needs.

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
# These test the core service function directly, bypassing HTTP.
# fetch_hired_candidates and try_send_signup_approved_email are always mocked
# so no real hiring portal or Brevo account is needed.

def test_new_candidate_creates_employee_and_user(db_only):
    # Happy path: a hired candidate should result in one Employee + one User row
    candidates = [_candidate()]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates), \
         patch("app.services.hiring_sync_service.try_send_signup_approved_email", return_value=True):

        result = run_sync(db_only)

    # Summary counts
    assert result["imported"] == 1
    assert result["skipped"] == 0
    assert result["errors"] == 0

    # Employee row stored correctly
    emp = db_only.query(Employee).filter(Employee.email == "alice@example.com").first()
    assert emp is not None
    assert emp.name == "Alice Smith"
    assert emp.designation == "Developer"
    assert emp.employee_type == "Full-time"

    # User row linked to Employee, active, with a hashed password (never plaintext)
    user = db_only.query(User).filter(User.email == "alice@example.com").first()
    assert user is not None
    assert user.employee_id == emp.id   # FK correctly linked
    assert user.is_active is True
    assert user.password_hash != ""     # bcrypt hash stored, not empty


def test_email_sent_flag_in_result(db_only):
    # Verify the email function is called with the right arguments
    # and that email_sent=True appears in the imported record
    candidates = [_candidate()]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates), \
         patch("app.services.hiring_sync_service.try_send_signup_approved_email", return_value=True) as mock_email:

        result = run_sync(db_only)

    # Email must be called exactly once per imported candidate
    mock_email.assert_called_once()
    call_kwargs = mock_email.call_args.kwargs
    assert call_kwargs["to_email"] == "alice@example.com"
    assert call_kwargs["to_name"] == "Alice Smith"
    # Password passed to email must be the same one stored in the DB (10 chars by default)
    assert len(call_kwargs["temp_password"]) == 10

    assert result["details"]["imported"][0]["email_sent"] is True


def test_email_failure_does_not_roll_back_user(db_only):
    # If Brevo is down, the account must still be created.
    # The candidate can reset their password later — losing the account entirely is worse.
    candidates = [_candidate()]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates), \
         patch("app.services.hiring_sync_service.try_send_signup_approved_email", return_value=False):

        result = run_sync(db_only)

    # Account created despite email failure
    assert result["imported"] == 1
    assert db_only.query(User).filter(User.email == "alice@example.com").first() is not None
    # Result honestly reports email was not sent
    assert result["details"]["imported"][0]["email_sent"] is False


def test_duplicate_via_employee_table_is_skipped(db_only):
    # Normal duplicate case: the candidate's email already exists in the employees table.
    # Should skip silently, not create a second row or crash.
    candidates = [_candidate()]

    # Seed an existing Employee before the sync runs
    existing_emp = Employee(
        name="Alice Smith", email="alice@example.com",
        employee_type="Full-time", status="active",
    )
    db_only.add(existing_emp)
    db_only.commit()

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates), \
         patch("app.services.hiring_sync_service.try_send_signup_approved_email", return_value=True):

        result = run_sync(db_only)

    assert result["imported"] == 0
    assert result["skipped"] == 1
    assert result["details"]["skipped"][0]["email"] == "alice@example.com"


def test_duplicate_via_user_table_is_skipped(db_only):
    # Edge case: Employee row was manually deleted but the User row still exists.
    # Without this check, creating a new User would cause a unique-email constraint crash.
    candidates = [_candidate()]

    # Only a User row exists — no matching Employee row
    existing_user = User(
        name="Alice Smith", email="alice@example.com",
        password_hash="irrelevant", role="employee", is_active=True,
    )
    db_only.add(existing_user)
    db_only.commit()

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates), \
         patch("app.services.hiring_sync_service.try_send_signup_approved_email", return_value=True):

        result = run_sync(db_only)

    # Should skip gracefully, not crash with IntegrityError
    assert result["imported"] == 0
    assert result["skipped"] == 1


def test_non_hired_candidate_is_ignored(db_only):
    # Candidates still in interview/screening must not be imported.
    # Only hr_status == "hired" triggers account creation.
    candidates = [_candidate(hr_status="interviewing")]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates), \
         patch("app.services.hiring_sync_service.try_send_signup_approved_email", return_value=True):

        result = run_sync(db_only)

    # Not imported, not skipped, not an error — just silently ignored
    assert result["imported"] == 0
    assert result["skipped"] == 0
    assert result["errors"] == 0
    assert db_only.query(Employee).count() == 0


def test_missing_email_recorded_as_error(db_only):
    # Bad data from the hiring portal (empty email) must not crash the whole sync.
    # It should be recorded in errors[] and the loop continues to the next candidate.
    candidates = [_candidate(email="")]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates), \
         patch("app.services.hiring_sync_service.try_send_signup_approved_email", return_value=True):

        result = run_sync(db_only)

    assert result["errors"] == 1
    assert result["details"]["errors"][0]["reason"] == "Missing name or email"


def test_missing_name_recorded_as_error(db_only):
    # Same as above but for empty name — both fields are required to create an account
    candidates = [_candidate(name="")]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates), \
         patch("app.services.hiring_sync_service.try_send_signup_approved_email", return_value=True):

        result = run_sync(db_only)

    assert result["errors"] == 1


def test_job_type_mapping(db_only):
    # "internship" from the hiring portal must map to "Intern" employee_type
    # "Reviewer" job_title must map to "Reviewer" designation (valid known title)
    candidates = [_candidate(job_type="internship", job_title="Reviewer")]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates), \
         patch("app.services.hiring_sync_service.try_send_signup_approved_email", return_value=True):

        run_sync(db_only)

    emp = db_only.query(Employee).filter(Employee.email == "alice@example.com").first()
    assert emp.employee_type == "Intern"
    assert emp.designation == "Reviewer"


def test_unknown_job_title_defaults_to_annotator(db_only):
    # If the hiring portal sends a job title we don't recognise,
    # fall back to "Annotator" rather than storing garbage or crashing
    candidates = [_candidate(job_title="Wizard")]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates), \
         patch("app.services.hiring_sync_service.try_send_signup_approved_email", return_value=True):

        run_sync(db_only)

    emp = db_only.query(Employee).filter(Employee.email == "alice@example.com").first()
    assert emp.designation == "Annotator"


def test_multiple_candidates_mixed_result(db_only):
    # Real-world sync will have a mix: some imported, some not-hired, some bad data.
    # Verifies the loop handles all three in one pass without aborting early.
    candidates = [
        _candidate(name="Alice", email="alice@x.com", application_id="A1"),               # should import
        _candidate(name="Bob",   email="bob@x.com",   application_id="A2", hr_status="interviewing"),  # ignored
        _candidate(name="",      email="bad@x.com",   application_id="A3"),                # error
    ]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates), \
         patch("app.services.hiring_sync_service.try_send_signup_approved_email", return_value=True):

        result = run_sync(db_only)

    assert result["imported"] == 1   # Alice only
    assert result["skipped"] == 0    # Bob was ignored (not hired), not counted as skipped
    assert result["errors"] == 1     # bad@x.com — missing name


# ── API tests: /preview and /sync endpoints ───────────────────────────────────
# These tests call the actual FastAPI endpoints via HTTP (TestClient).
# They verify that the API layer correctly delegates to the service layer
# and converts RuntimeError → HTTP 502 when the hiring portal is unreachable.
#
# Note: preview patches app.api.hiring_sync.fetch_hired_candidates (import site),
#       sync patches app.services.hiring_sync_service.fetch_hired_candidates (definition site).
#       This is because preview calls it directly in the route handler,
#       while sync goes through run_sync() which calls it internally.

def test_preview_endpoint_shows_would_import(client_and_db):
    # Fresh DB — candidate does not exist yet — preview should say would_import=True
    client, _ = client_and_db
    candidates = [_candidate()]

    with patch("app.api.hiring_sync.fetch_hired_candidates", return_value=candidates):
        r = client.get("/api/hiring/preview")

    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    assert data["candidates"][0]["would_import"] is True
    assert data["candidates"][0]["skip_reason"] is None


def test_preview_shows_skip_reason_for_existing(client_and_db):
    # Candidate already in DB — preview must report would_import=False with a reason
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
    # POST /api/hiring/sync should return the imported/skipped/errors counts
    client, _ = client_and_db
    candidates = [_candidate()]

    with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=candidates), \
         patch("app.services.hiring_sync_service.try_send_signup_approved_email", return_value=True):

        r = client.post("/api/hiring/sync")

    assert r.status_code == 200
    body = r.json()
    assert body["imported"] == 1
    assert body["skipped"] == 0
    assert body["errors"] == 0


def test_sync_endpoint_returns_502_when_hiring_portal_unreachable(client_and_db):
    # If the hiring portal is down, the API must return 502 Bad Gateway
    # (not 500, not 200 with empty results)
    client, _ = client_and_db

    with patch(
        "app.services.hiring_sync_service.fetch_hired_candidates",
        side_effect=RuntimeError("Cannot reach hiring portal: Connection refused"),
    ):
        r = client.post("/api/hiring/sync")

    assert r.status_code == 502
    assert "Cannot reach hiring portal" in r.json()["detail"]


def test_preview_endpoint_returns_502_when_hiring_portal_unreachable(client_and_db):
    # Same 502 behaviour for the preview endpoint
    client, _ = client_and_db

    with patch(
        "app.services.hiring_sync_service.fetch_hired_candidates",
        side_effect=RuntimeError("Cannot reach hiring portal: Connection refused"),
    ):
        r = client.get("/api/hiring/preview")

    assert r.status_code == 502
