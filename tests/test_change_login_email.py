"""Self-service login-email change, restricted to the company domain.

Employees can move their login onto their real @autonexai360.com address. The
login row (users.email) has to move with it, and the password must survive, since
the confirmation email promises "same password". These tests pin all three, plus
the rule that the generic employee update can't be used to dodge the domain check.
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
import app.models.allocation       # noqa: F401
import app.models.email_otp        # noqa: F401
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

import app.api.employees as employees_api
from app.api.employees import COMPANY_EMAIL_DOMAIN, router as employees_router
from app.services.auth_service import get_current_user

employees_api.hash_password = lambda pw: "hashed-pw"

# Capture the OTP email notice instead of sending it.
notices = []
employees_api.try_send_otp_email = lambda **kw: (notices.append(kw), True)[1]

PERSONAL = "person@gmail.com"
WORK = f"person@{COMPANY_EMAIL_DOMAIN}"


@pytest.fixture()
def ctx():
    """TestClient plus a session, an employee with a login, and a role switch."""
    notices.clear()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    db = TestingSessionLocal()
    employee = Employee(name="Test Person", email=PERSONAL, employee_type="Full-time",
                        designation="Developer", status="active", skills=[])
    db.add(employee)
    db.flush()
    login = User(name="Test Person", email=PERSONAL, password_hash="original-hash",
                 role="employee", employee_id=employee.id, is_active=True, skills=[])
    db.add(login)
    db.commit()

    # Default caller is the employee themselves; tests can flip to an admin.
    state = {"user": login}

    app = FastAPI()
    app.include_router(employees_router)
    app.dependency_overrides[database.get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: state["user"]

    def as_admin():
        state["user"] = User(id=999, email="admin@x.com", name="Admin",
                            role="admin", is_active=True)

    yield TestClient(app), db, employee, as_admin
    db.close()
    Base.metadata.drop_all(bind=engine)


def change_email(client, employee_id, new_email):
    notices.clear()
    resp1 = client.post(f"/api/employees/{employee_id}/email/request", json={"new_email": new_email})
    if resp1.status_code != 200:
        return resp1
    otp = notices[-1]["otp"]
    return client.post(f"/api/employees/{employee_id}/email/verify", json={"otp": otp})


def test_accepts_an_address_outside_the_company_domain(ctx):
    client, db, employee, _ = ctx

    resp = change_email(client, employee.id, "another@gmail.com")

    assert resp.status_code == 200
    db.expire_all()
    assert db.query(Employee).get(employee.id).email == "another@gmail.com"
    assert len(notices) == 1


def test_accepts_the_company_domain_and_moves_the_login(ctx):
    client, db, employee, _ = ctx

    resp = change_email(client, employee.id, WORK)

    assert resp.status_code == 200
    assert resp.json()["email"] == WORK
    db.expire_all()
    assert db.query(Employee).get(employee.id).email == WORK
    # Login reads users.email — if this didn't move, the person is locked out.
    login = db.query(User).filter(User.employee_id == employee.id).first()
    assert login.email == WORK
    assert login.password_hash == "original-hash"


def test_confirmation_notice_goes_to_the_new_address(ctx):
    client, _, employee, _ = ctx

    change_email(client, employee.id, WORK)

    assert len(notices) == 1
    assert notices[0]["to_email"] == WORK
    assert "otp" in notices[0]


def test_uppercase_input_is_normalised(ctx):
    client, db, employee, _ = ctx

    resp = change_email(client, employee.id, WORK.upper())

    assert resp.status_code == 200
    db.expire_all()
    # Login compares exactly, so a stray capital would break sign-in.
    assert db.query(User).filter(User.employee_id == employee.id).first().email == WORK


def test_rejects_an_address_another_person_already_uses(ctx):
    client, db, employee, _ = ctx
    db.add(Employee(name="Someone Else", email=WORK, employee_type="Full-time", status="active"))
    db.commit()

    resp = change_email(client, employee.id, WORK)

    assert resp.status_code == 409
    db.expire_all()
    assert db.query(Employee).get(employee.id).email == PERSONAL


def test_rejects_a_no_op_change(ctx):
    client, db, employee, as_admin = ctx
    as_admin()
    client.put(f"/api/employees/{employee.id}", json={"email": WORK})

    resp = change_email(client, employee.id, WORK)

    assert resp.status_code == 400


def test_employee_cannot_dodge_the_otp_rule_via_the_generic_update(ctx):
    """Without this guard the whole OTP verification requirement is decorative."""
    client, db, employee, _ = ctx

    resp = client.put(f"/api/employees/{employee.id}", json={"email": "anything@elsewhere.com"})

    assert resp.status_code == 403
    assert "email" in resp.json()["detail"]
    db.expire_all()
    assert db.query(Employee).get(employee.id).email == PERSONAL


def test_admin_can_still_set_any_address_via_the_generic_update(ctx):
    client, db, employee, as_admin = ctx
    as_admin()

    resp = client.put(f"/api/employees/{employee.id}", json={"email": "contractor@partner.com"})

    assert resp.status_code == 200
    db.expire_all()
    assert db.query(Employee).get(employee.id).email == "contractor@partner.com"
