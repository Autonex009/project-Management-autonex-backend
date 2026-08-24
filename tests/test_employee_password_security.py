"""Tests for secure random temporary password generation and forced password change workflow."""
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

from app.models.employee import Employee
from app.models.user import User
from app.models.signup_request import SignupRequest

from app.api.signup_requests import router as signup_requests_router
from app.api.employees import router as employees_router
from app.api.auth import router as auth_router
import app.api.auth as auth_api
import app.api.employees as employees_api
import app.services.auth_service as auth_service
from app.services.auth_service import hash_password, get_current_user


@pytest.fixture()
def client_and_db():
    auth_api.verify_password = auth_service.verify_password
    auth_api.hash_password = auth_service.hash_password
    employees_api.hash_password = auth_service.hash_password

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
    app.include_router(signup_requests_router)
    app.include_router(employees_router)
    app.include_router(auth_router)

    app.dependency_overrides[database.get_db] = override_get_db

    db = TestingSessionLocal()
    # Create default admin user
    admin_user = User(
        id=1,
        name="Admin User",
        email="admin@autonexai360.com",
        password_hash=hash_password("admin_pass_123"),
        role="admin",
        is_active=True,
    )
    db.add(admin_user)
    db.commit()
    db.refresh(admin_user)

    app.dependency_overrides[get_current_user] = lambda: admin_user

    yield TestClient(app), db, app, TestingSessionLocal
    db.close()
    Base.metadata.drop_all(bind=engine)


def test_create_employee_random_password_and_forced_reset(client_and_db):
    client, db, app, TestingSessionLocal = client_and_db

    payload = {
        "name": "Karan Sharma",
        "email": "karan.sharma@autonexai360.com",
        "employee_type": "Full-time",
        "designation": "Annotator",
        "skills": ["Python", "Annotation"],
        "working_hours_per_day": 8.0,
        "weekly_availability": 40.0,
        "productivity_baseline": 1.0,
        "status": "active",
    }

    res = client.post("/api/employees", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()

    # 1. Verify response includes temporary credentials
    temp_password = data.get("temp_password")
    assert temp_password is not None
    assert len(temp_password) >= 8
    assert temp_password != "emp123"  # Must NOT be legacy static password

    # 2. Verify user in database has must_change_password=True
    user = db.query(User).filter(User.email == payload["email"]).first()
    assert user is not None
    assert user.must_change_password is True

    # 3. Verify login fails with legacy "emp123"
    bad_login = client.post("/api/auth/login", json={"email": payload["email"], "password": "emp123"})
    assert bad_login.status_code == 401

    # 4. Verify login succeeds with temporary password
    good_login = client.post("/api/auth/login", json={"email": payload["email"], "password": temp_password})
    assert good_login.status_code == 200
    login_data = good_login.json()
    assert login_data["user"]["must_change_password"] is True

    # 5. Verify forced change password endpoint for employee
    app.dependency_overrides[get_current_user] = lambda: user
    change_res = client.post(
        "/api/auth/change-password",
        json={"new_password": "NewSecurePassword#2026"},
    )
    assert change_res.status_code == 200
    user_updated = change_res.json()
    assert user_updated["must_change_password"] is False

    # 6. Verify login with new password
    new_login = client.post("/api/auth/login", json={"email": payload["email"], "password": "NewSecurePassword#2026"})
    assert new_login.status_code == 200
    assert new_login.json()["user"]["must_change_password"] is False


def test_signup_approval_forced_password_reset(client_and_db):
    client, db, app, TestingSessionLocal = client_and_db

    # Create signup request
    req = SignupRequest(
        name="Pooja Patel",
        email="pooja.patel@autonexai360.com",
        phone="9876543210",
        designation="Annotator",
        employee_type="Full-time",
        skills=["Annotation"],
        status="pending",
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    # Approve signup
    res = client.patch(f"/api/signup-requests/{req.id}/approve")
    assert res.status_code == 200

    approved_user = db.query(User).filter(User.email == req.email).first()
    assert approved_user is not None
    assert approved_user.must_change_password is True
