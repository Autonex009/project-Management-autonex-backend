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
    from app.services.auth_service import get_current_user
    def override_get_current_user():
        return User(id=1, email="admin@x.com", name="Admin", role="admin", is_active=True)

    app.dependency_overrides[database.get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    db = TestingSessionLocal()
    yield TestClient(app), db
    db.close()
    Base.metadata.drop_all(bind=engine)


def _seed(db):
    """Create an admin user, a PM (user + employee), and return their ids."""
    admin_emp = Employee(name="Admin Person", email="admin@x.com", status="active",
                         employee_type="employee", slack_user_id="U123ADMIN")
    pm_emp = Employee(name="Pat Manager", email="pm@x.com", status="active",
                      employee_type="employee", designation="Project Manager",
                      razorpay_email="pm@x.com", slack_user_id="U123PM")
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
            "reason": "Test reason",
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


def test_employee_wfh_routes_to_pm(client_and_db):
    from app.models.parent_project import MainProject
    from app.models.sub_project import SubProject
    from app.models.project import DailySheet
    from app.models.allocation import Allocation

    client, db = client_and_db
    ids = _seed(db)
    pm_emp_id = ids["pm_emp"].id
    admin_user_id = ids["admin_user"].id

    # Create a normal employee
    emp = Employee(name="Jane Employee", email="jane@x.com", status="active",
                   employee_type="employee")
    db.add(emp)
    db.commit()
    db.refresh(emp)

    emp_user = User(email="jane@x.com", password_hash="x", name="Jane Employee",
                    role="employee", employee_id=emp.id, is_active=True)
    db.add(emp_user)
    db.commit()
    db.refresh(emp_user)

    wfh_date = _next_weekday(date.today() + timedelta(days=15))

    # Create MainProject, SubProject, DailySheet, and Allocation
    main_proj = MainProject(
        name="Main Proj",
        program_manager_id=pm_emp_id,
        client="Client X",
        global_start_date=date.today() - timedelta(days=30),
        status="active"
    )
    db.add(main_proj)
    db.commit()
    db.refresh(main_proj)

    sub_proj = SubProject(
        main_project_id=main_proj.id,
        name="Sub Proj",
        client="Client X",
        pm_id=pm_emp_id,
        start_date=date.today() - timedelta(days=30),
        duration_days=90,
        status="active"
    )
    db.add(sub_proj)
    db.commit()
    db.refresh(sub_proj)

    sheet = DailySheet(
        name="Daily Sheet X",
        client="Client X",
        project_type="Annotation",
        total_tasks=1000,
        estimated_time_per_task=0.5,
        sub_project_id=sub_proj.id,
        main_project_id=main_proj.id,
        start_date=date.today() - timedelta(days=30),
        end_date=date.today() + timedelta(days=60),
        priority="medium",
        project_status="active"
    )
    db.add(sheet)
    db.commit()
    db.refresh(sheet)

    alloc = Allocation(
        employee_id=emp.id,
        sub_project_id=sheet.id,
        total_daily_hours=8,
        active_start_date=date.today() - timedelta(days=30),
        active_end_date=date.today() + timedelta(days=60),
    )
    db.add(alloc)
    db.commit()

    # Now Jane applies for WFH
    resp = client.post("/api/wfh", json={
        "employee_id": emp.id,
        "wfh_date": wfh_date.isoformat(),
        "reason": "WFH study time",
    })
    assert resp.status_code == 201, resp.text
    wfh = resp.json()
    assert wfh["status"] == "pending"

    # Verify PM received the in-app notification (wfh_applied)
    pm_notifs = db.query(Notification).filter(
        Notification.user_id == ids["pm_user"].id,
        Notification.type == "wfh_applied",
    ).all()
    assert len(pm_notifs) == 1, "PM should receive a notification for their allocated employee's WFH"

    # Verify admin did NOT receive it (because it was routed to PM)
    admin_notifs = db.query(Notification).filter(
        Notification.user_id == admin_user_id,
        Notification.type == "wfh_applied",
    ).all()
    assert len(admin_notifs) == 0, "Admin should not receive notification since request went to PM"


def test_wfh_limits_fulltime(client_and_db):
    client, db = client_and_db
    ids = _seed(db)
    emp = Employee(name="FT Employee", email="ft@x.com", status="active",
                   employee_type="Full-time")
    db.add(emp)
    db.commit()
    db.refresh(emp)

    # Tuesday and Wednesday of next week
    next_mon = date.today() + timedelta(days=7 - date.today().weekday())
    wfh_date1 = next_mon + timedelta(days=1)  # Tuesday
    wfh_date2 = next_mon + timedelta(days=2)  # Wednesday

    # First request: should pass
    resp = client.post("/api/wfh", json={
        "employee_id": emp.id,
        "wfh_date": wfh_date1.isoformat(),
        "reason": "Reason 1",
    })
    assert resp.status_code == 201, resp.text

    # Second request in the same week: should fail (limit 1/week)
    resp = client.post("/api/wfh", json={
        "employee_id": emp.id,
        "wfh_date": wfh_date2.isoformat(),
        "reason": "Reason 2",
    })
    assert resp.status_code == 400, resp.text
    assert "limited to 1 WFH day per week" in resp.json()["detail"]


def test_wfh_limits_intern_and_contractor(client_and_db):
    client, db = client_and_db
    ids = _seed(db)
    
    # Test Intern
    intern = Employee(name="Intern Employee", email="intern@x.com", status="active",
                      employee_type="Intern")
    db.add(intern)
    db.commit()
    db.refresh(intern)

    base = date.today() + timedelta(days=20)
    wfh_dates = []
    d = base
    while len(wfh_dates) < 3:
        d = _next_weekday(d)
        wfh_dates.append(d)
        d += timedelta(days=1)

    # Ensure all are in the same calendar month
    if wfh_dates[0].month != wfh_dates[-1].month:
        base = (date.today() + timedelta(days=40)).replace(day=1)
        wfh_dates = []
        d = base
        while len(wfh_dates) < 3:
            d = _next_weekday(d)
            wfh_dates.append(d)
            d += timedelta(days=1)

    # First WFH
    resp = client.post("/api/wfh", json={
        "employee_id": intern.id,
        "wfh_date": wfh_dates[0].isoformat(),
        "reason": "Reason 1",
    })
    assert resp.status_code == 201, resp.text

    # Second WFH
    resp = client.post("/api/wfh", json={
        "employee_id": intern.id,
        "wfh_date": wfh_dates[1].isoformat(),
        "reason": "Reason 2",
    })
    assert resp.status_code == 201, resp.text

    # Third WFH (should fail - limit 2/month)
    resp = client.post("/api/wfh", json={
        "employee_id": intern.id,
        "wfh_date": wfh_dates[2].isoformat(),
        "reason": "Reason 3",
    })
    assert resp.status_code == 400, resp.text
    assert "limited to 2 WFH days per calendar month" in resp.json()["detail"]

    # Test Contractor
    contractor = Employee(name="Contractor Employee", email="contractor@x.com", status="active",
                          employee_type="Contractor")
    db.add(contractor)
    db.commit()
    db.refresh(contractor)

    # First WFH
    resp = client.post("/api/wfh", json={
        "employee_id": contractor.id,
        "wfh_date": wfh_dates[0].isoformat(),
        "reason": "Reason 1",
    })
    assert resp.status_code == 201, resp.text

    # Second WFH
    resp = client.post("/api/wfh", json={
        "employee_id": contractor.id,
        "wfh_date": wfh_dates[1].isoformat(),
        "reason": "Reason 2",
    })
    assert resp.status_code == 201, resp.text

    # Third WFH (should fail)
    resp = client.post("/api/wfh", json={
        "employee_id": contractor.id,
        "wfh_date": wfh_dates[2].isoformat(),
        "reason": "Reason 3",
    })
    assert resp.status_code == 400, resp.text
    assert "limited to 2 WFH days per calendar month" in resp.json()["detail"]

