"""
Test Suite: Payroll Identity Sync and Duplicate Prevention
===========================================================
Verifies that:
1. Converting an Intern to Full-time immediately reflects as "Full-time" on the Payroll tab.
2. Updating an employee's name updates the existing Salary record in place and never creates duplicates.
3. Multiple name modifications preserve single-record mapping by employee ID / normalized identity.
4. Legacy duplicate zero-pay records are automatically healed and pruned.
"""
import os
os.environ.pop("SLACK_BOT_TOKEN", None)
from cryptography.fernet import Fernet
os.environ["SALARY_KEY"] = Fernet.generate_key().decode()
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db.database as database
from app.db.database import Base
from app.models.employee import Employee
from app.models.user import User
from app.models.payroll import Salary
from app.api.employees import router as employees_router
from app.api.payroll import router as payroll_router
from app.services.salary_crypto import encrypt_salary


@pytest.fixture()
def client_and_db(monkeypatch):
    monkeypatch.setattr("app.services.project_cache.get_redis_client", lambda: None)
    monkeypatch.setattr("app.services.auth_service.get_redis_client", lambda: None)
    monkeypatch.setattr("app.services.slack_service.try_get_or_cache_employee_slack_user_id", lambda db, emp: None)
    monkeypatch.setattr("app.services.slack_service.try_notify_employee_promoted_fulltime", lambda **kwargs: None)

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
    app.include_router(payroll_router)

    from app.services.auth_service import get_current_user
    def override_get_current_user():
        return User(id=1, email="admin@autonexai360.com", name="Admin User", role="admin", is_active=True)

    app.dependency_overrides[database.get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    db = TestingSessionLocal()
    yield TestClient(app), db
    db.close()
    Base.metadata.drop_all(bind=engine)


def test_name_update_updates_existing_salary_without_duplicate(client_and_db):
    client, db = client_and_db

    # 1. Create employee
    emp = Employee(
        name="Reefa Kachhi",
        email="reefa@autonexai360.com",
        employee_type="Intern",
        status="active",
        base_salary_enc=encrypt_salary(25000.0),
    )
    db.add(emp)
    db.commit()
    db.refresh(emp)

    user = User(
        email=emp.email,
        name=emp.name,
        role="employee",
        employee_id=emp.id,
        password_hash="fakehash",
        is_active=True,
    )
    db.add(user)
    db.commit()

    # 2. Add salary record for Reefa Kachhi
    db.add(Salary(
        id=1,
        employee_id=emp.id,
        full_name="Reefa Kachhi",
        status="Active",
        employment_type="Intern",
        base_pay_monthly=encrypt_salary(25000.0),
        opt_bonus_monthly=None,
    ))
    db.commit()

    # Initial check: exactly 1 salary record
    resp = client.get("/api/payroll/salary-records")
    assert resp.status_code == 200
    salaries = resp.json()["salaries"]
    assert len(salaries) == 1
    assert salaries[0]["full_name"] == "Reefa Kachhi"
    assert salaries[0]["base_pay_monthly"] == "₹25,000"

    # 3. Update employee name to "Reefa K." via PUT /api/employees/{id}
    update_resp = client.put(f"/api/employees/{emp.id}", json={"name": "Reefa K."})
    assert update_resp.status_code == 200
    assert update_resp.json()["name"] == "Reefa K."

    # 4. Check /api/payroll/salary-records again
    # MUST have exactly 1 record, with full_name "Reefa K.", keeping the 25,000 pay, ZERO orphans
    resp2 = client.get("/api/payroll/salary-records")
    assert resp2.status_code == 200
    salaries2 = resp2.json()["salaries"]
    assert len(salaries2) == 1, f"Expected 1 record, got {len(salaries2)}: {salaries2}"
    assert salaries2[0]["full_name"] == "Reefa K."
    assert salaries2[0]["employee_id"] == emp.id
    assert salaries2[0]["base_pay_monthly"] == "₹25,000"

    # 5. Multiple updates: rename to "Reefa Kachhi-Jena"
    update_resp2 = client.put(f"/api/employees/{emp.id}", json={"name": "Reefa Kachhi-Jena"})
    assert update_resp2.status_code == 200

    resp3 = client.get("/api/payroll/salary-records")
    assert resp3.status_code == 200
    salaries3 = resp3.json()["salaries"]
    assert len(salaries3) == 1
    assert salaries3[0]["full_name"] == "Reefa Kachhi-Jena"
    assert salaries3[0]["employee_id"] == emp.id
    assert salaries3[0]["base_pay_monthly"] == "₹25,000"


def test_intern_conversion_syncs_employment_type_to_payroll(client_and_db):
    client, db = client_and_db

    emp = Employee(
        name="Alex intern",
        email="alex@autonexai360.com",
        employee_type="Intern",
        status="active",
        base_salary_enc=encrypt_salary(20000.0),
    )
    db.add(emp)
    db.commit()
    db.refresh(emp)

    user = User(
        email=emp.email,
        name=emp.name,
        role="employee",
        employee_id=emp.id,
        password_hash="fakehash",
        is_active=True,
    )
    db.add(user)
    db.commit()

    db.add(Salary(
        id=2,
        employee_id=emp.id,
        full_name="Alex intern",
        status="Active",
        employment_type="Intern",
        base_pay_monthly=encrypt_salary(20000.0),
    ))
    db.commit()

    # Verify initial state is "Intern"
    resp = client.get("/api/payroll/salary-records")
    assert resp.status_code == 200
    assert resp.json()["salaries"][0]["employment_type"] == "Intern"

    # Convert to Full-time via Pathway B (profile update)
    conv_resp = client.put(f"/api/employees/{emp.id}", json={"employee_type": "Full-time"})
    assert conv_resp.status_code == 200
    assert conv_resp.json()["employee_type"] == "Full-time"

    # Check /api/payroll/salary-records immediately reflects "Full-time"
    resp2 = client.get("/api/payroll/salary-records")
    assert resp2.status_code == 200
    assert resp2.json()["salaries"][0]["employment_type"] == "Full-time"

    # Check monthly pay preview also reflects "Full-time"
    preview_resp = client.get("/api/payroll/preview", params={"month": "2026-03"})
    assert preview_resp.status_code == 200
    preview_emp = next(e for e in preview_resp.json()["employees"] if e["employee_id"] == emp.id)
    assert preview_emp["employee_type"] == "Full-time"


def test_self_heal_prunes_legacy_zero_pay_duplicate(client_and_db):
    client, db = client_and_db

    emp = Employee(
        name="Sam Specialist",
        email="sam@autonexai360.com",
        employee_type="Full-time",
        status="active",
        base_salary_enc=encrypt_salary(35000.0),
    )
    db.add(emp)
    db.commit()
    db.refresh(emp)

    # Simulate legacy bug: two rows exist for Sam in the salary table!
    # Row 1: non-zero pay, older name
    # Row 2: 0 pay duplicate created by name change
    db.add_all([
        Salary(
            id=10,
            employee_id=emp.id,
            full_name="Sam Specialist",
            status="Active",
            employment_type="Full-time",
            base_pay_monthly=encrypt_salary(35000.0),
        ),
        Salary(
            id=11,
            employee_id=emp.id,
            full_name="Sam Specialist",
            status="Active",
            employment_type="Full-time",
            base_pay_monthly=encrypt_salary(0.0),
        ),
    ])
    db.commit()

    # When /api/payroll/salary-records is called, self-healing prunes the zero-pay row
    resp = client.get("/api/payroll/salary-records")
    assert resp.status_code == 200
    salaries = resp.json()["salaries"]
    assert len(salaries) == 1
    assert salaries[0]["base_pay_monthly"] == "₹35,000"
