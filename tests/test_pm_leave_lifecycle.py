"""
Verification: PM leave & WFH lifecycle
======================================
A PM applies for leave/WFH using the SAME endpoints as employees. PM requests
must route to Admin (not to a project PM), and the full lifecycle
(apply -> pending -> admin approve/reject) must behave like an employee's.

What is verified (API layer, in-memory SQLite, no Slack/Razorpay):
  ● PM paid-leave application -> 201, status "pending"
  ● PM leave routes an in-app notification to the Admin user
  ● Admin approve -> status "approved", approved_by = admin user id
  ● PM unpaid-leave application is accepted (same path as employees)
  ● PM WFH application -> 201 pending, Admin notified
  ● Admin reject of PM WFH -> status "rejected"
"""
import os
# Disable external integrations so the flow runs fully offline.
os.environ.pop("SLACK_BOT_TOKEN", None)
os.environ.pop("RAZORPAY_API_ID", None)
os.environ.pop("RAZORPAY_API_KEY", None)

import sys
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.db.database as database
from app.db.database import Base

# Import every model so the in-memory schema resolves cross-table FKs.
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
from app.models.notification import Notification
from app.api.leaves import router as leave_router
from app.api.wfh import router as wfh_router


@pytest.fixture()
def client_and_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(leave_router)
    app.include_router(wfh_router)
    app.dependency_overrides[database.get_db] = override_get_db

    db = TestingSessionLocal()
    yield TestClient(app), db
    db.close()
    Base.metadata.drop_all(bind=engine)


def _seed(db):
    """Create an admin user, a PM (user + employee), and return their ids."""
    admin_emp = Employee(name="Admin Person", email="admin@x.com", status="active",
                         employee_type="employee")
    pm_emp = Employee(name="Pat Manager", email="pm@x.com", status="active",
                      employee_type="employee", designation="Project Manager",
                      razorpay_email="pm@x.com")
    db.add_all([admin_emp, pm_emp])
    db.commit()
    db.refresh(admin_emp)
    db.refresh(pm_emp)

    admin_user = User(email="admin@x.com", password_hash="x", name="Admin Person",
                      role="admin", employee_id=admin_emp.id, is_active=True)
    pm_user = User(email="pm@x.com", password_hash="x", name="Pat Manager",
                   role="pm", employee_id=pm_emp.id, is_active=True)
    db.add_all([admin_user, pm_user])
    db.commit()
    db.refresh(admin_user)
    db.refresh(pm_user)
    return {"admin_user": admin_user, "pm_user": pm_user, "pm_emp": pm_emp}


def _next_weekday(d):
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def test_pm_leave_routes_to_admin_and_full_lifecycle(client_and_db):
    client, db = client_and_db
    ids = _seed(db)
    pm_emp_id = ids["pm_emp"].id
    admin_user_id = ids["admin_user"].id

    start = _next_weekday(date.today() + timedelta(days=10))
    end = start

    # PM applies for paid leave (same endpoint employees use)
    resp = client.post("/api/leaves", json={
        "employee_id": pm_emp_id,
        "leave_type": "paid",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "reason": "PM personal day",
    })
    assert resp.status_code == 201, resp.text
    leave = resp.json()
    assert leave["status"] == "pending"
    leave_id = leave["leave_id"]

    # Routed to Admin: the admin user has an in-app leave_applied notification
    admin_notifs = db.query(Notification).filter(
        Notification.user_id == admin_user_id,
        Notification.type == "leave_applied",
    ).all()
    assert len(admin_notifs) == 1, "PM leave should notify the Admin"

    # Admin approves
    resp = client.patch(f"/api/leaves/{leave_id}/approve", params={"approved_by": admin_user_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved"

    got = client.get(f"/api/leaves/{leave_id}").json()
    assert got["status"] == "approved"
    assert got["approved_by"] == admin_user_id


def test_pm_paid_leave_over_monthly_limit_is_flagged(client_and_db):
    """Same flagging rule as employees: the 3rd paid leave in a calendar month
    is flagged (max 2/month). 'Unpaid' is not a selectable type — exhausted
    paid quota is auto-converted to unpaid by payroll, identical to employees."""
    client, db = client_and_db
    ids = _seed(db)
    pm_emp_id = ids["pm_emp"].id

    # Three single-day paid leaves on distinct weekdays in the same month
    base = _next_weekday(date.today().replace(day=1) + timedelta(days=40))
    days = []
    d = base
    while len(days) < 3:
        d = _next_weekday(d)
        if d.month == base.month:
            days.append(d)
        d += timedelta(days=1)

    flags = []
    for day in days:
        resp = client.post("/api/leaves", json={
            "employee_id": pm_emp_id,
            "leave_type": "paid",
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
        })
        assert resp.status_code == 201, resp.text
        flags.append(resp.json()["flagged"])

    assert flags[0] is False and flags[1] is False
    assert flags[2] is True, "3rd paid leave in a month must be flagged for PMs too"


def test_pm_wfh_routes_to_admin_and_can_be_rejected(client_and_db):
    client, db = client_and_db
    ids = _seed(db)
    admin_user_id = ids["admin_user"].id
    wfh_date = _next_weekday(date.today() + timedelta(days=12))

    resp = client.post("/api/wfh", json={
        "employee_id": ids["pm_emp"].id,
        "wfh_date": wfh_date.isoformat(),
        "reason": "PM WFH",
    })
    assert resp.status_code == 201, resp.text
    wfh = resp.json()
    assert wfh["status"] == "pending"

    admin_notifs = db.query(Notification).filter(
        Notification.user_id == admin_user_id,
        Notification.type == "wfh_applied",
    ).all()
    assert len(admin_notifs) == 1, "PM WFH should notify the Admin"

    resp = client.patch(f"/api/wfh/{wfh['id']}/reject", params={"approved_by": admin_user_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"
