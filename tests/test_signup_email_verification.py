"""Signup email verification (two-step signup).

Signup used to accept any typed address, so an applicant who mistyped their email
was approved and then couldn't log in — an admin had to fix the address by hand.
Now step 1 mails a link to the address and step 2 reads the email out of that
signed link, so the address on the admin queue is always one that receives mail.

These tests pin that contract: the body can never decide the email.
"""
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.db.database as database
from app.db.database import Base
import app.models.employee         # noqa: F401
import app.models.notification     # noqa: F401
import app.models.signup_request   # noqa: F401
import app.models.user             # noqa: F401

from app.models.employee import Employee
from app.models.signup_request import SignupRequest
from app.models.user import User

import app.api.signup_requests as signup_api
from app.api.signup_requests import router as signup_router

# Capture verification emails instead of sending them.
sent_emails = []
signup_api.send_signup_verification_email = lambda **kw: sent_emails.append(kw)


@pytest.fixture()
def client_and_db():
    sent_emails.clear()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(signup_router)
    app.dependency_overrides[database.get_db] = override_get_db
    db = TestingSessionLocal()
    yield TestClient(app), db
    db.close()
    Base.metadata.drop_all(bind=engine)


def token_for(email):
    return signup_api._create_verify_token(email)


# ── Step 1: request the link ──────────────────────────────────────────────────

def test_step1_emails_a_link_to_the_address(client_and_db):
    client, _ = client_and_db

    resp = client.post("/api/signup-requests/verify-email", json={"email": "New.Person@autonex.com"})

    assert resp.status_code == 200
    # Normalised, so the address is comparable everywhere downstream.
    assert resp.json()["email"] == "new.person@autonex.com"
    assert len(sent_emails) == 1
    assert sent_emails[0]["to_email"] == "new.person@autonex.com"
    # The link must carry a token, otherwise step 2 is unreachable.
    assert "token=" in sent_emails[0]["signup_link"]


def test_step1_rejects_an_address_that_already_has_an_account(client_and_db):
    client, db = client_and_db
    db.add(Employee(name="Existing", email="taken@autonex.com", employee_type="Full-time", status="active"))
    db.commit()

    resp = client.post("/api/signup-requests/verify-email", json={"email": "taken@autonex.com"})

    assert resp.status_code == 409
    # Told before filling in the form, and no link handed out.
    assert sent_emails == []


# ── Token checking ────────────────────────────────────────────────────────────

def test_check_returns_the_verified_address(client_and_db):
    client, _ = client_and_db

    resp = client.get("/api/signup-requests/verify-email/check",
                      params={"token": token_for("someone@autonex.com")})

    assert resp.status_code == 200
    assert resp.json() == {"email": "someone@autonex.com", "verified": True}


def test_check_rejects_a_tampered_token(client_and_db):
    client, _ = client_and_db
    tampered = token_for("someone@autonex.com")[:-4] + "AAAA"

    resp = client.get("/api/signup-requests/verify-email/check", params={"token": tampered})

    assert resp.status_code == 400


# ── Step 2: submit ────────────────────────────────────────────────────────────

def test_submit_without_a_token_is_rejected(client_and_db):
    client, _ = client_and_db

    resp = client.post("/api/signup-requests", json={
        "name": "No Token",
        "email": "notoken@autonex.com",
        "designation": "Developer",
    })

    assert resp.status_code == 422


def test_submit_uses_the_tokens_email_not_the_body(client_and_db):
    """The bug this whole flow exists to prevent: a body-supplied address winning."""
    client, db = client_and_db

    resp = client.post("/api/signup-requests", json={
        "name": "Kisan",
        "verification_token": token_for("kisan123@autonex.com"),
        "email": "kisan12@autonex.com",          # typo'd address a client might send
        "designation": "Developer",
    })

    assert resp.status_code == 201
    assert resp.json()["email"] == "kisan123@autonex.com"
    stored = db.query(SignupRequest).all()
    assert [r.email for r in stored] == ["kisan123@autonex.com"]


def test_submit_with_a_tampered_token_is_rejected(client_and_db):
    client, db = client_and_db
    tampered = token_for("someone@autonex.com")[:-4] + "AAAA"

    resp = client.post("/api/signup-requests", json={
        "name": "Forged",
        "verification_token": tampered,
    })

    assert resp.status_code == 400
    assert db.query(SignupRequest).count() == 0


def test_the_same_link_cannot_be_submitted_twice(client_and_db):
    client, db = client_and_db
    token = token_for("once@autonex.com")

    first = client.post("/api/signup-requests", json={"name": "Real Person", "verification_token": token})
    second = client.post("/api/signup-requests", json={"name": "Impostor", "verification_token": token})

    assert first.status_code == 201
    assert second.status_code == 409
    # Only the first submission survives.
    assert [r.name for r in db.query(SignupRequest).all()] == ["Real Person"]


def test_a_rejected_applicant_can_reapply(client_and_db):
    client, db = client_and_db
    db.add(SignupRequest(name="Second Chance", email="retry@autonex.com",
                         employee_type="Full-time", status="rejected"))
    db.commit()

    resp = client.post("/api/signup-requests", json={
        "name": "Second Chance",
        "verification_token": token_for("retry@autonex.com"),
    })

    assert resp.status_code == 201
    rows = db.query(SignupRequest).filter(SignupRequest.email == "retry@autonex.com").all()
    assert [r.status for r in rows] == ["pending"]
