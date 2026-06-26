"""
Verification: convert intern → full-time in place
==================================================
Promotion updates the SAME employee row (preserving every linked record),
flips only employment type, records an audit trail (when + who), and — via the
payroll cutoff — never retroactively reclassifies leave taken while an intern.
"""
import os
os.environ.pop("SLACK_BOT_TOKEN", None)

import sys
from datetime import date

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
from app.models.leave import Leave
from app.models.user import User
from app.models.notification import Notification
from app.api.employees import router as employees_router
from app.api.payroll import _classify_year_leaves


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
    app.dependency_overrides[database.get_db] = override_get_db
    db = TestingSessionLocal()
    yield TestClient(app), db
    db.close()
    Base.metadata.drop_all(bind=engine)


def _seed_intern(db):
    emp = Employee(name="Tara Trainee", email="tara@x.com", employee_type="Intern",
                   designation="Annotator", status="active", base_salary=18000.0)
    db.add(emp)
    db.commit()
    db.refresh(emp)
    user = User(email="tara@x.com", password_hash="x", name="Tara Trainee",
                role="employee", employee_id=emp.id, is_active=True)
    admin = User(email="admin@x.com", password_hash="x", name="Boss", role="admin", is_active=True)
    db.add_all([user, admin])
    db.commit()
    db.refresh(user)
    db.refresh(admin)
    return emp, user, admin


def test_convert_preserves_record_and_audits(client_and_db):
    client, db = client_and_db
    emp, user, admin = _seed_intern(db)
    emp_id = emp.id

    # Historical linked record that must survive the conversion.
    db.add(Leave(employee_id=emp_id, leave_type="paid",
                 start_date=date(2026, 1, 5), end_date=date(2026, 1, 5), status="approved"))
    db.commit()

    resp = client.post(f"/api/employees/{emp_id}/convert-to-fulltime", json={"converted_by": admin.id})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Same identity, only type changed; audit recorded.
    assert body["id"] == emp_id
    assert body["employee_type"] == "Full-time"
    assert body["previous_employee_type"] == "Intern"
    assert body["converted_by"] == admin.id
    assert body["converted_to_fulltime_at"] is not None

    # Linked history preserved (same employee_id).
    db.expire_all()
    leaves = db.query(Leave).filter(Leave.employee_id == emp_id).all()
    assert len(leaves) == 1 and leaves[0].start_date == date(2026, 1, 5)

    # Auditable in-app notification to the employee.
    notifs = db.query(Notification).filter(
        Notification.user_id == user.id, Notification.type == "employee_converted"
    ).all()
    assert len(notifs) == 1


def test_convert_twice_rejected(client_and_db):
    client, db = client_and_db
    emp, _user, admin = _seed_intern(db)
    first = client.post(f"/api/employees/{emp.id}/convert-to-fulltime", json={"converted_by": admin.id})
    assert first.status_code == 200
    second = client.post(f"/api/employees/{emp.id}/convert-to-fulltime", json={"converted_by": admin.id})
    assert second.status_code == 400
    assert "Only interns" in second.json()["detail"]


def test_payroll_cutoff_does_not_reclassify_pre_promotion_leave():
    """Promotion effective 2026-03-01: Jan leaves keep the monthly intern rule;
    April leaves (post-promotion) use the annual quota."""
    class _L:
        def __init__(self, id, start, leave_type="paid"):
            self.id = id; self.start_date = start; self.end_date = start
            self.leave_type = leave_type; self.status = "approved"

    leaves = [
        _L(1, date(2026, 1, 5)),   # intern period
        _L(2, date(2026, 1, 6)),   # intern period (2nd in month → unpaid)
        _L(3, date(2026, 4, 6)),   # full-time period
        _L(4, date(2026, 4, 7)),   # full-time period (annual quota → still paid)
    ]
    cls, balances = _classify_year_leaves(leaves, 2026, intern=False, intern_until=date(2026, 3, 1))

    # Pre-promotion: monthly rule preserved (1 paid, 1 unpaid in Jan).
    assert cls[1]["paid_dates"] and not cls[1]["unpaid_dates"]
    assert not cls[2]["paid_dates"] and cls[2]["unpaid_dates"]
    # Post-promotion: annual quota — both April days paid.
    assert cls[3]["paid_dates"] and not cls[3]["unpaid_dates"]
    assert cls[4]["paid_dates"] and not cls[4]["unpaid_dates"]
    # Now full-time → annual paid balance shown.
    assert balances["paid"]["period"] == "year"
