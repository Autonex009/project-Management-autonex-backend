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
import app.models.admin            # noqa: F401
import app.models.allocation       # noqa: F401
import app.models.employee         # noqa: F401
import app.models.guideline        # noqa: F401
import app.models.leave            # noqa: F401
import app.models.notification     # noqa: F401
import app.models.parent_project   # noqa: F401
import app.models.payroll          # noqa: F401
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
from app.models.signup_request import SignupRequest

from app.api.signup_requests import router as signup_requests_router
from app.api.employees import router as employees_router
from app.api.auth import router as auth_router

import app.api.employees as employees_api
import app.api.signup_requests as signup_requests_api
import app.api.auth as auth_api
import app.services.auth_service as auth_service

# Stub password hashing/verification so passlib/bcrypt combination issues in python-bcrypt do not break tests
employees_api.hash_password = lambda pw: "hashed-pw"
signup_requests_api.bcrypt = type('dummy', (object,), {
    'hashpw': lambda pw, salt: b"hashed-pw",
    'gensalt': lambda: b"dummy-salt"
})
auth_api.hash_password = lambda pw: "hashed-pw"
auth_api.verify_password = lambda plain, hashed: True


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
    app.include_router(signup_requests_router)
    app.include_router(employees_router)
    app.include_router(auth_router)
    
    app.dependency_overrides[database.get_db] = override_get_db
    db = TestingSessionLocal()
    yield TestClient(app), db
    db.close()
    Base.metadata.drop_all(bind=engine)


def test_signup_request_duplicate_email(client_and_db):
    client, db = client_and_db
    
    # 1. Submit initial signup request
    resp = client.post("/api/signup-requests", json={
        "name": "Arjun Mehta",
        "email": "arjun@autonex.com",
        "phone": "+91 98765 43210",
        "designation": "Developer"
    })
    assert resp.status_code == 201

    # 2. Try to submit duplicate request with same email
    resp2 = client.post("/api/signup-requests", json={
        "name": "Arjun Duplicate",
        "email": "arjun@autonex.com",
        "phone": "9999999999",
        "designation": "Developer"
    })
    assert resp2.status_code == 409
    assert "already" in resp2.json()["detail"]

    # 3. Try to submit duplicate request with same email but uppercase
    resp3 = client.post("/api/signup-requests", json={
        "name": "Arjun Duplicate",
        "email": "ARJUN@AUTONEX.COM",
        "phone": "9999999999",
        "designation": "Developer"
    })
    assert resp3.status_code == 409


def test_signup_request_duplicate_phone(client_and_db):
    client, db = client_and_db
    
    # 1. Submit initial signup request
    resp = client.post("/api/signup-requests", json={
        "name": "Rahul Verma",
        "email": "rahul@autonex.com",
        "phone": "+91-98765-43210",
        "designation": "Developer"
    })
    assert resp.status_code == 201

    # 2. Try to submit duplicate request with formatted duplicate phone number
    resp2 = client.post("/api/signup-requests", json={
        "name": "Rahul Duplicate",
        "email": "rahul.diff@autonex.com",
        "phone": "9876543210",
        "designation": "Developer"
    })
    assert resp2.status_code == 409
    assert "phone number" in resp2.json()["detail"]


def test_create_employee_duplicate_email_or_phone(client_and_db):
    client, db = client_and_db
    
    # 1. Create an employee
    resp = client.post("/api/employees", json={
        "name": "Vinayak Shukla",
        "email": "vinayak@autonex.com",
        "phone": "9876543210",
        "employee_type": "Full-time"
    })
    assert resp.status_code == 200

    # 2. Try to create duplicate employee with same email
    resp2 = client.post("/api/employees", json={
        "name": "Vinayak Duplicate Email",
        "email": "vinayak@autonex.com",
        "phone": "1111111111",
        "employee_type": "Full-time"
    })
    assert resp2.status_code == 409

    # 3. Try to create duplicate employee with same phone (different formatting)
    resp3 = client.post("/api/employees", json={
        "name": "Vinayak Duplicate Phone",
        "email": "vinayak.diff@autonex.com",
        "phone": "+91-98765-43210",
        "employee_type": "Full-time"
    })
    assert resp3.status_code == 409


def test_update_employee_duplicate(client_and_db):
    client, db = client_and_db
    
    # 1. Create Employee A and B
    emp_a = client.post("/api/employees", json={
        "name": "Employee A",
        "email": "a@autonex.com",
        "phone": "9876543210",
        "employee_type": "Full-time"
    }).json()
    
    emp_b = client.post("/api/employees", json={
        "name": "Employee B",
        "email": "b@autonex.com",
        "phone": "1111111111",
        "employee_type": "Full-time"
    }).json()

    # 2. Try to update B to have A's email (banned)
    resp_update_email = client.put(f"/api/employees/{emp_b['id']}", json={
        "email": "a@autonex.com"
    })
    assert resp_update_email.status_code == 409

    # 3. Try to update B to have A's phone (banned)
    resp_update_phone = client.put(f"/api/employees/{emp_b['id']}", json={
        "phone": "+91 98765 43210"
    })
    assert resp_update_phone.status_code == 409


def test_auth_signup_duplicate_email(client_and_db):
    client, db = client_and_db
    
    # 1. Create Employee A
    client.post("/api/employees", json={
        "name": "Employee A",
        "email": "a@autonex.com",
        "employee_type": "Full-time"
    })

    # 2. Try to sign up via public signup auth endpoint with duplicate email
    resp = client.post("/api/auth/signup", json={
        "name": "Register Duplicate",
        "email": "a@autonex.com",
        "password": "mypassword123",
        "role": "employee"
    })
    assert resp.status_code == 409


def test_auto_link_prevents_duplicate_user(client_and_db):
    client, db = client_and_db
    
    # 1. Create an Employee
    emp = Employee(
        name="Linked Employee",
        email="linked@autonex.com",
        employee_type="Full-time",
        status="active"
    )
    db.add(emp)
    db.commit()

    # 2. Register User A linked to this Employee
    user_a = User(
        name="User A",
        email="user_a@autonex.com",
        password_hash="pw",
        role="employee",
        employee_id=emp.id
    )
    db.add(user_a)
    db.commit()

    # 3. Register User B with same email as Employee (not linked yet)
    user_b = User(
        name="User B",
        email="linked@autonex.com",
        password_hash="pw",
        role="employee",
        employee_id=None
    )
    db.add(user_b)
    db.commit()

    # 4. Try to login User B (which triggers auto-linking to the Employee)
    # This should fail because the Employee is already linked to User A!
    resp = client.post("/api/auth/login", json={
        "email": "linked@autonex.com",
        "password": "pw"
    })
    assert resp.status_code == 409
    assert "already linked" in resp.json()["detail"]


def test_update_employee_with_existing_signup_request(client_and_db):
    client, db = client_and_db
    
    # 1. Submit signup request
    signup_resp = client.post("/api/signup-requests", json={
        "name": "Jane Doe",
        "email": "jane@autonex.com",
        "phone": "9876543219",
        "designation": "Developer",
        "employee_type": "Full-time"
    })
    assert signup_resp.status_code == 201
    signup_id = signup_resp.json()["id"]

    # 2. Approve it to create the Employee + User
    approve_resp = client.patch(f"/api/signup-requests/{signup_id}/approve")
    assert approve_resp.status_code == 200
    employee_id = approve_resp.json()["employee_id"]

    # 3. Update the employee (e.g. set status to inactive, sending the email and phone)
    update_resp = client.put(f"/api/employees/{employee_id}", json={
        "name": "Jane Doe",
        "email": "jane@autonex.com",
        "phone": "9876543219",
        "employee_type": "Full-time",
        "status": "inactive"
    })
    assert update_resp.status_code == 200
    assert update_resp.json()["status"] == "inactive"

