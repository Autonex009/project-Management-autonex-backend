"""
Verification: payroll endpoints are gated by PAYROLL_PASSCODE
=============================================================
When PAYROLL_PASSCODE is set, payroll API calls without the correct passcode are
rejected (401) — so payroll data is unreachable regardless of the UI. When unset,
the gate is disabled (dev convenience).
"""
import os
os.environ.pop("SLACK_BOT_TOKEN", None)

import sys
import hashlib

# Server stores only the SHA-256 hash; clients send the plaintext passcode.
PASSCODE = "s3cret-pay"
PASSCODE_HASH = hashlib.sha256(PASSCODE.encode()).hexdigest()

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.db.database as database
from app.db.database import Base

import app.models.allocation       # noqa: F401
import app.models.employee         # noqa: F401
import app.models.guideline        # noqa: F401
import app.models.leave            # noqa: F401
import app.models.notification     # noqa: F401
import app.models.parent_project   # noqa: F401
import app.models.payroll          # noqa: F401
import app.models.project          # noqa: F401
import app.models.referral         # noqa: F401
import app.models.side_project     # noqa: F401
import app.models.signup_request   # noqa: F401
import app.models.skill            # noqa: F401
import app.models.sub_project      # noqa: F401
import app.models.user             # noqa: F401
import app.models.wfh              # noqa: F401

from app.api.payroll import router as payroll_router


@pytest.fixture()
def client():
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
    app.include_router(payroll_router)
    from app.services.auth_service import get_current_user
    from app.models.user import User
    def override_get_current_user():
        return User(id=1, email="admin@x.com", name="Admin", role="admin", is_active=True)

    app.dependency_overrides[database.get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    yield TestClient(app)
    Base.metadata.drop_all(bind=engine)


def test_gate_disabled_when_passcode_unset(client, monkeypatch):
    monkeypatch.delenv("PAYROLL_PASSCODE_HASH", raising=False)
    r = client.get("/api/payroll/preview", params={"month": "2026-06"})
    assert r.status_code == 200, r.text          # open in dev (no passcode configured)


def test_blocks_without_passcode_when_set(client, monkeypatch):
    monkeypatch.setenv("PAYROLL_PASSCODE_HASH", PASSCODE_HASH)
    r = client.get("/api/payroll/preview", params={"month": "2026-06"})
    assert r.status_code == 401


def test_blocks_with_wrong_passcode(client, monkeypatch):
    monkeypatch.setenv("PAYROLL_PASSCODE_HASH", PASSCODE_HASH)
    r = client.get("/api/payroll/preview", params={"month": "2026-06"},
                   headers={"X-Payroll-Passcode": "wrong"})
    assert r.status_code == 401


def test_allows_with_correct_passcode_header(client, monkeypatch):
    monkeypatch.setenv("PAYROLL_PASSCODE_HASH", PASSCODE_HASH)
    r = client.get("/api/payroll/preview", params={"month": "2026-06"},
                   headers={"X-Payroll-Passcode": PASSCODE})
    assert r.status_code == 200, r.text


def test_csv_allows_passcode_via_query(client, monkeypatch):
    monkeypatch.setenv("PAYROLL_PASSCODE_HASH", PASSCODE_HASH)
    blocked = client.get("/api/payroll/export.csv", params={"month": "2026-06"})
    assert blocked.status_code == 401
    ok = client.get("/api/payroll/export.csv", params={"month": "2026-06", "passcode": PASSCODE})
    assert ok.status_code == 200, ok.text
