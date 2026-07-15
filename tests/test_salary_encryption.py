"""
Verification: salary is encrypted at rest and never exposed in plaintext
========================================================================
- base_salary written via the employees API is stored ONLY as ciphertext in
  base_salary_enc; the plaintext column stays NULL.
- The general employee API responses never include base_salary.
- The admin payroll path decrypts it (with the key) to compute salary.
- Without the key, ciphertext is unreadable (decrypt → None).
"""
import os
os.environ.pop("SLACK_BOT_TOKEN", None)
from cryptography.fernet import Fernet
os.environ["SALARY_KEY"] = Fernet.generate_key().decode()

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
import app.api.employees as employees_api
from app.api.employees import router as employees_router
from app.services import salary_crypto

# The pinned passlib/bcrypt combo raises in this Python's bcrypt backend when
# hashing — unrelated to salary encryption. Stub it so the API create path runs.
employees_api.hash_password = lambda pw: "test-hashed-password"


# ── Pure crypto unit tests ────────────────────────────────────────────────────

def test_round_trip():
    token = salary_crypto.encrypt_salary(54321.0)
    assert isinstance(token, str) and token != "54321.0"
    assert salary_crypto.decrypt_salary(token) == 54321.0


def test_decrypt_without_key_returns_none(monkeypatch):
    token = salary_crypto.encrypt_salary(1000.0)
    monkeypatch.delenv("SALARY_KEY", raising=False)
    assert salary_crypto.decrypt_salary(token) is None      # no key → unreadable
    assert salary_crypto.encrypt_salary(1000.0) is None     # no key → write dropped


def test_decrypt_garbage_returns_none():
    assert salary_crypto.decrypt_salary("not-a-real-token") is None
    assert salary_crypto.decrypt_salary(None) is None


# ── API-level tests ───────────────────────────────────────────────────────────

@pytest.fixture()
def client_and_db():
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
    app.include_router(employees_router)
    from app.services.auth_service import get_current_user
    from app.models.user import User
    def override_get_current_user():
        return User(id=1, email="admin@x.com", name="Admin", role="admin", is_active=True)

    app.dependency_overrides[database.get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    db = TestingSessionLocal()
    yield TestClient(app), db
    db.close()
    Base.metadata.drop_all(bind=engine)


def test_create_encrypts_and_response_hides_salary(client_and_db):
    client, db = client_and_db
    resp = client.post("/api/employees", json={
        "name": "Sal Aried", "email": "sal@x.com", "employee_type": "Full-time",
        "base_salary": 75000,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Response never carries the salary.
    assert "base_salary" not in body

    # Stored as ciphertext, plaintext column NULL.
    emp = db.query(Employee).filter(Employee.id == body["id"]).first()
    assert emp.base_salary is None
    assert emp.base_salary_enc and "75000" not in emp.base_salary_enc
    assert salary_crypto.decrypt_salary(emp.base_salary_enc) == 75000.0


def test_update_salary_is_encrypted(client_and_db):
    client, db = client_and_db
    emp_id = client.post("/api/employees", json={
        "name": "Eve", "email": "eve@x.com", "employee_type": "Full-time",
    }).json()["id"]

    resp = client.put(f"/api/employees/{emp_id}", json={"base_salary": 90000})
    assert resp.status_code == 200, resp.text
    assert "base_salary" not in resp.json()

    emp = db.query(Employee).filter(Employee.id == emp_id).first()
    assert emp.base_salary is None
    assert salary_crypto.decrypt_salary(emp.base_salary_enc) == 90000.0

    # GET list also hides it.
    listed = client.get("/api/employees").json()
    assert all("base_salary" not in e for e in listed)
