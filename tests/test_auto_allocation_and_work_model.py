"""Tests for:
1. 7-Working-Day Continuous Streak Auto-Allocation for Idle Employees
2. Monthly Work Model Auto-Adjustment (WFO <-> WFH)
"""
import os
import sys
from datetime import date, timedelta, datetime

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.database import Base
from app.models.employee import Employee
from app.models.user import User
from app.models.project import DailySheet as Project
from app.models.allocation import Allocation
from app.models.daily_checkin import DailyCheckIn
from app.models.leave import Leave
from app.models.audit_log import AuditLog

import app.services.allocation_service as allocation_service
from app.constants.leave_types import is_fixed_holiday, is_weekend
from app.services.allocation_service import (
    check_employee_project_streak,
    sync_employee_allocations_from_checkin,
)
from app.services.work_model_service import sync_monthly_work_models


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


_checkin_counter = 0

def make_checkin(**kwargs):
    global _checkin_counter
    _checkin_counter += 1
    if "id" not in kwargs:
        kwargs["id"] = _checkin_counter
    return DailyCheckIn(**kwargs)


def _latest_working_day(d):
    """Walk back to the nearest day the streak logic counts as worked."""
    while is_weekend(d) or is_fixed_holiday(d):
        d -= timedelta(days=1)
    return d


def _working_days_ending(anchor, count):
    """The `count` most recent working days up to and including `anchor`, oldest first.

    Derived with the same weekend/holiday helpers the streak check uses, so the
    fixture cannot drift from the rule it is exercising.
    """
    days = []
    d = anchor
    while len(days) < count:
        if not (is_weekend(d) or is_fixed_holiday(d)):
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


@pytest.fixture()
def today(monkeypatch):
    """Pin the service clock to the most recent working day.

    sync_employee_allocations_from_checkin() reads the wall clock
    (datetime.utcnow()) rather than taking a date, so a test that seeds check-ins
    around a fixed calendar date only passes on that date. These tests previously
    hard-coded 2026-09-16 and had been failing since the day after it. Anchoring
    to the real calendar and freezing the service clock to the same day keeps them
    deterministic on any run date, weekends and holidays included.
    """
    anchor = _latest_working_day(date.today())

    class _FrozenDateTime(datetime):
        @classmethod
        def utcnow(cls):
            return datetime(anchor.year, anchor.month, anchor.day, 12, 0, 0)

    monkeypatch.setattr(allocation_service, "datetime", _FrozenDateTime)
    return anchor


def test_idle_employee_1_day_checkin_does_not_allocate(db, today):
    """An idle employee checking in on Day 1 should NOT be auto-allocated."""
    emp = Employee(name="John Doe", email="john@example.com", employee_type="Full-time", designation="Annotator", status="active")
    db.add(emp)
    db.flush()

    user = User(name="John Doe", email="john@example.com", password_hash="dummy", role="employee", employee_id=emp.id)
    db.add(user)

    proj = Project(name="Project Alpha", client="TestClient", project_type="Annotation", total_tasks=100, estimated_time_per_task=1.0, start_date=date(2026, 1, 1), project_status="active")
    db.add(proj)
    db.commit()

    chk = make_checkin(employee_id=emp.id, checkin_date=today, work_mode="WFO", project_ids=[proj.id])
    db.add(chk)
    db.commit()

    bg = BackgroundTasks()
    sync_employee_allocations_from_checkin(
        db=db,
        employee_id=emp.id,
        submitted_project_ids=[proj.id],
        background_tasks=bg,
        http_request=None,
    )

    allocs = db.query(Allocation).filter(Allocation.employee_id == emp.id, Allocation.is_active == True).all()
    assert len(allocs) == 0, "Idle employee should not be allocated on Day 1"


def test_idle_employee_7_consecutive_working_days_auto_allocates(db, today):
    """An idle employee checking in for 7 consecutive working days on the same project gets auto-allocated."""
    emp = Employee(name="Streak Hero", email="hero@example.com", employee_type="Full-time", designation="Annotator", status="active")
    db.add(emp)
    db.flush()

    user = User(name="Streak Hero", email="hero@example.com", password_hash="dummy", role="employee", employee_id=emp.id)
    db.add(user)

    proj = Project(name="Alpha Project", client="TestClient", project_type="Annotation", total_tasks=100, estimated_time_per_task=1.0, start_date=date(2026, 1, 1), project_status="active")
    db.add(proj)
    db.commit()

    # A check-in on each of the working days leading up to and including today, so
    # the 7-day streak lands on today whenever the suite happens to run.
    for d in _working_days_ending(today, 8):
        chk = make_checkin(employee_id=emp.id, checkin_date=d, work_mode="WFO", project_ids=[proj.id])
        db.add(chk)
    db.commit()

    # Verify helper directly
    streak_met = check_employee_project_streak(db, emp.id, proj.id, today, required_streak=7)
    assert streak_met is True, "Streak should be satisfied for 7 working days"

    # Trigger allocation sync
    bg = BackgroundTasks()
    sync_employee_allocations_from_checkin(
        db=db,
        employee_id=emp.id,
        submitted_project_ids=[proj.id],
        background_tasks=bg,
        http_request=None,
    )

    # Assert allocation is created and active
    allocs = db.query(Allocation).filter(Allocation.employee_id == emp.id, Allocation.is_active == True).all()
    assert len(allocs) == 1, "Should have created 1 permanent allocation"
    alloc = allocs[0]
    assert alloc.sub_project_id == proj.id
    assert alloc.total_daily_hours == 8
    assert alloc.is_active is True

    # Assert audit log is recorded
    audit = db.query(AuditLog).filter(
        AuditLog.action == "allocation.auto_created",
        AuditLog.subject_employee_id == emp.id,
    ).first()
    assert audit is not None, "Audit log must be created"
    assert "7-day continuous check-in streak" in audit.summary

    # Assert background tasks include project sync and Slack notification
    assert len(bg.tasks) == 2
    task_func_names = [t.func.__name__ for t in bg.tasks]
    assert "_background_sync_projects" in task_func_names
    assert "_background_notify_allocation" in task_func_names


def test_streak_broken_by_missing_working_day(db, today):
    """If employee misses a working day check-in, streak is broken and no allocation occurs."""
    emp = Employee(name="Missing Person", email="miss@example.com", employee_type="Full-time", designation="Annotator", status="active")
    db.add(emp)
    db.flush()

    user = User(name="Missing Person", email="miss@example.com", password_hash="dummy", role="employee", employee_id=emp.id)
    db.add(user)

    proj = Project(name="Beta Project", client="TestClient", project_type="Annotation", total_tasks=100, estimated_time_per_task=1.0, start_date=date(2026, 1, 1), project_status="active")
    db.add(proj)
    db.commit()

    # Only the last three working days, so the run of 7 is broken by the gap before them.
    for d in _working_days_ending(today, 3):
        db.add(make_checkin(employee_id=emp.id, checkin_date=d, work_mode="WFO", project_ids=[proj.id]))
    db.commit()

    streak_met = check_employee_project_streak(db, emp.id, proj.id, today, required_streak=7)
    assert streak_met is False

    bg = BackgroundTasks()
    sync_employee_allocations_from_checkin(db, emp.id, [proj.id], bg, None)

    allocs = db.query(Allocation).filter(Allocation.employee_id == emp.id, Allocation.is_active == True).all()
    assert len(allocs) == 0


def test_pm_lead_admin_never_auto_allocated(db, today):
    """PMs, Leads, and Admins are excluded from auto-allocation even with a 7-day streak."""
    for role in ["pm", "team_lead", "admin"]:
        emp = Employee(name=f"Lead {role}", email=f"{role}@example.com", employee_type="Full-time", designation="Lead", status="active")
        db.add(emp)
        db.flush()

        user = User(name=f"Lead {role}", email=f"{role}@example.com", password_hash="dummy", role=role, employee_id=emp.id)
        db.add(user)

        proj = Project(name=f"Proj {role}", client="TestClient", project_type="Annotation", total_tasks=100, estimated_time_per_task=1.0, start_date=date(2026, 1, 1), project_status="active")
        db.add(proj)
        db.commit()

        # A full 7-working-day streak ending today — the allocation must still be
        # refused on role grounds, not because the streak was unmet.
        for d in _working_days_ending(today, 7):
            db.add(make_checkin(employee_id=emp.id, checkin_date=d, work_mode="WFO", project_ids=[proj.id]))
        db.commit()

        bg = BackgroundTasks()
        sync_employee_allocations_from_checkin(db, emp.id, [proj.id], bg, None)

        allocs = db.query(Allocation).filter(Allocation.employee_id == emp.id, Allocation.is_active == True).all()
        assert len(allocs) == 0, f"Role {role} should never be auto-allocated"


def test_monthly_work_model_switches_to_wfh(db):
    """If an employee works 100% WFH on all working days in a month (>= 15 days), switch work_model to WFH."""
    emp = Employee(name="Remote Worker", email="remote@example.com", employee_type="Full-time", designation="Developer", status="active", work_model="WFO")
    db.add(emp)
    db.commit()
    emp.created_at = datetime(2026, 8, 1)
    db.commit()

    # Target date: Oct 1, 2026 -> evaluates September 2026
    # Create 20 working day check-ins in September 2026 with work_mode="WFH"
    cur = date(2026, 9, 1)
    month_end = date(2026, 9, 30)
    while cur <= month_end:
        if cur.weekday() < 5:  # Mon-Fri
            db.add(make_checkin(employee_id=emp.id, checkin_date=cur, work_mode="WFH", project_ids=[1]))
        cur += timedelta(days=1)
    db.commit()

    result = sync_monthly_work_models(db, target_date=date(2026, 10, 1), min_threshold_days=15)

    assert result["switched_to_wfh_count"] == 1
    assert "Remote Worker" in result["switched_to_wfh"]

    db.refresh(emp)
    assert emp.work_model == "WFH"

    # Audit log verified
    audit = db.query(AuditLog).filter(
        AuditLog.action == "employee.work_model_auto_switched",
        AuditLog.subject_employee_id == emp.id,
    ).first()
    assert audit is not None
    assert "WFO to WFH" in audit.summary


def test_monthly_work_model_mixed_mode_no_switch(db):
    """An employee who worked both WFO and WFH should NOT have their work_model changed."""
    emp = Employee(name="Hybrid Worker", email="hybrid@example.com", employee_type="Full-time", designation="Developer", status="active", work_model="WFO")
    db.add(emp)
    db.commit()
    emp.created_at = datetime(2026, 8, 1)
    db.commit()

    # September 2026: 10 WFH days and 10 WFO days
    cur = date(2026, 9, 1)
    month_end = date(2026, 9, 30)
    wfh_count = 0
    while cur <= month_end:
        if cur.weekday() < 5:
            mode = "WFH" if wfh_count < 10 else "WFO"
            db.add(make_checkin(employee_id=emp.id, checkin_date=cur, work_mode=mode, project_ids=[1]))
            wfh_count += 1
        cur += timedelta(days=1)
    db.commit()

    result = sync_monthly_work_models(db, target_date=date(2026, 10, 1), min_threshold_days=15)

    assert result["switched_to_wfh_count"] == 0
    assert result["switched_to_wfo_count"] == 0

    db.refresh(emp)
    assert emp.work_model == "WFO"


def test_pm_and_lead_receive_slack_on_allocation(db, monkeypatch):
    """When an employee is allocated to a project, PM and Team Lead receive a Slack notification."""
    from app.services.slack_service import send_allocation_notifications_to_leaders

    # Create PM employee
    pm_emp = Employee(name="Manager Mike", email="mike@example.com", employee_type="Full-time", designation="Project Manager", status="active", slack_user_id="U_PM123")
    db.add(pm_emp)
    db.flush()

    # Create Team Lead employee
    lead_emp = Employee(name="Lead Lisa", email="lisa@example.com", employee_type="Full-time", designation="Team Lead", status="active", slack_user_id="U_LEAD456")
    db.add(lead_emp)
    db.flush()

    # Create Project with PM assigned
    proj = Project(
        name="Project Phoenix",
        client="TestClient",
        project_type="Annotation",
        total_tasks=100,
        estimated_time_per_task=1.0,
        start_date=date(2026, 1, 1),
        project_status="active",
        assigned_employee_ids=[pm_emp.id],
    )
    db.add(proj)
    db.flush()

    # Lead allocation on Project
    lead_alloc = Allocation(
        employee_id=lead_emp.id,
        sub_project_id=proj.id,
        total_daily_hours=8,
        role_tags=["Team Lead"],
        is_active=True,
    )
    db.add(lead_alloc)
    db.flush()

    # New member to allocate
    new_member = Employee(name="Worker Bob", email="bob@example.com", employee_type="Full-time", designation="Annotator", status="active", slack_user_id="U_BOB789")
    db.add(new_member)
    db.flush()

    new_alloc = Allocation(
        employee_id=new_member.id,
        sub_project_id=proj.id,
        total_daily_hours=8,
        is_active=True,
    )
    db.add(new_alloc)
    db.commit()

    # Track Slack notifications sent
    sent_notifications = []
    def mock_notify_leader(**kwargs):
        sent_notifications.append(kwargs)
        return True

    monkeypatch.setattr("app.services.slack_service.notify_project_leader_allocation_created", mock_notify_leader)

    # Call send_allocation_notifications_to_leaders
    send_allocation_notifications_to_leaders(db, new_alloc, proj, source="Manual Allocation")

    # Verify both PM and Lead were notified
    assert len(sent_notifications) == 2
    recipient_slack_ids = {n["leader_slack_user_id"] for n in sent_notifications}
    assert "U_PM123" in recipient_slack_ids
    assert "U_LEAD456" in recipient_slack_ids

    for n in sent_notifications:
        assert n["employee_name"] == "Worker Bob"
        assert n["sub_project_name"] == "Project Phoenix"
        assert n["allocated_hours_per_day"] == "8h/day"
        assert n["allocation_source"] == "Manual Allocation"


def test_admin_unallocates_notifies_pm_lead_and_employee(db, monkeypatch):
    """If admin unallocates someone: PM, Lead, and Employee receive notifications."""
    from app.services.slack_service import send_unallocation_notifications

    pm_emp = Employee(name="PM Pete", email="pete@example.com", employee_type="Full-time", designation="Project Manager", status="active", slack_user_id="U_PM_PETE")
    lead_emp = Employee(name="Lead Laura", email="laura@example.com", employee_type="Full-time", designation="Team Lead", status="active", slack_user_id="U_LEAD_LAURA")
    worker = Employee(name="Worker Dan", email="dan@example.com", employee_type="Full-time", designation="Annotator", status="active", slack_user_id="U_DAN")
    admin_emp = Employee(name="Admin Alice", email="alice@example.com", employee_type="Full-time", designation="Operations Admin", status="active", slack_user_id="U_ALICE")
    db.add_all([pm_emp, lead_emp, worker, admin_emp])
    db.flush()

    proj = Project(name="Project Omega", client="TestClient", project_type="Annotation", total_tasks=100, estimated_time_per_task=1.0, start_date=date(2026, 1, 1), project_status="active", assigned_employee_ids=[pm_emp.id])
    db.add(proj)
    db.flush()

    lead_alloc = Allocation(employee_id=lead_emp.id, sub_project_id=proj.id, total_daily_hours=8, role_tags=["Team Lead"], is_active=True)
    worker_alloc = Allocation(employee_id=worker.id, sub_project_id=proj.id, total_daily_hours=8, is_active=True)
    db.add_all([lead_alloc, worker_alloc])
    db.commit()

    leader_notifications = []
    employee_notifications = []
    monkeypatch.setattr("app.services.slack_service.notify_project_leader_allocation_removed", lambda **kwargs: leader_notifications.append(kwargs))
    monkeypatch.setattr("app.services.slack_service.notify_employee_allocation_removed", lambda **kwargs: employee_notifications.append(kwargs))

    alloc_info = {"id": worker_alloc.id, "employee_id": worker.id, "sub_project_id": proj.id, "total_daily_hours": 8, "role_tags": []}

    # Admin removes worker
    send_unallocation_notifications(
        db=db,
        alloc_info=alloc_info,
        project_id=proj.id,
        actor_user_role="admin",
        actor_employee_id=admin_emp.id,
        actor_name="Admin Alice",
    )

    # PM and Lead must both receive leader removal notification
    assert len(leader_notifications) == 2
    recipients = {n["leader_slack_user_id"] for n in leader_notifications}
    assert "U_PM_PETE" in recipients
    assert "U_LEAD_LAURA" in recipients

    # Employee must receive employee removal notification
    assert len(employee_notifications) == 1
    assert employee_notifications[0]["employee_slack_user_id"] == "U_DAN"


def test_pm_unallocates_notifies_lead_and_employee_only(db, monkeypatch):
    """If PM unallocates someone: Lead and Employee receive notifications (PM is not notified)."""
    from app.services.slack_service import send_unallocation_notifications

    pm_emp = Employee(name="PM Pete", email="pete2@example.com", employee_type="Full-time", designation="Project Manager", status="active", slack_user_id="U_PM_PETE2")
    lead_emp = Employee(name="Lead Laura", email="laura2@example.com", employee_type="Full-time", designation="Team Lead", status="active", slack_user_id="U_LEAD_LAURA2")
    worker = Employee(name="Worker Dan", email="dan2@example.com", employee_type="Full-time", designation="Annotator", status="active", slack_user_id="U_DAN2")
    db.add_all([pm_emp, lead_emp, worker])
    db.flush()

    proj = Project(name="Project Omega 2", client="TestClient", project_type="Annotation", total_tasks=100, estimated_time_per_task=1.0, start_date=date(2026, 1, 1), project_status="active", assigned_employee_ids=[pm_emp.id])
    db.add(proj)
    db.flush()

    lead_alloc = Allocation(employee_id=lead_emp.id, sub_project_id=proj.id, total_daily_hours=8, role_tags=["Team Lead"], is_active=True)
    worker_alloc = Allocation(employee_id=worker.id, sub_project_id=proj.id, total_daily_hours=8, is_active=True)
    db.add_all([lead_alloc, worker_alloc])
    db.commit()

    leader_notifications = []
    employee_notifications = []
    monkeypatch.setattr("app.services.slack_service.notify_project_leader_allocation_removed", lambda **kwargs: leader_notifications.append(kwargs))
    monkeypatch.setattr("app.services.slack_service.notify_employee_allocation_removed", lambda **kwargs: employee_notifications.append(kwargs))

    alloc_info = {"id": worker_alloc.id, "employee_id": worker.id, "sub_project_id": proj.id, "total_daily_hours": 8, "role_tags": []}

    # PM removes worker
    send_unallocation_notifications(
        db=db,
        alloc_info=alloc_info,
        project_id=proj.id,
        actor_user_role="pm",
        actor_employee_id=pm_emp.id,
        actor_name="PM Pete",
    )

    # ONLY Lead must receive leader removal notification
    assert len(leader_notifications) == 1
    assert leader_notifications[0]["leader_slack_user_id"] == "U_LEAD_LAURA2"
    assert leader_notifications[0]["remover_role"] == "Project Manager"

    # Employee must receive employee removal notification
    assert len(employee_notifications) == 1
    assert employee_notifications[0]["employee_slack_user_id"] == "U_DAN2"


def test_lead_unallocates_notifies_pm_and_employee_only(db, monkeypatch):
    """If Lead unallocates someone: PM and Employee receive notifications (Lead is not notified)."""
    from app.services.slack_service import send_unallocation_notifications

    pm_emp = Employee(name="PM Pete", email="pete3@example.com", employee_type="Full-time", designation="Project Manager", status="active", slack_user_id="U_PM_PETE3")
    lead_emp = Employee(name="Lead Laura", email="laura3@example.com", employee_type="Full-time", designation="Team Lead", status="active", slack_user_id="U_LEAD_LAURA3")
    worker = Employee(name="Worker Dan", email="dan3@example.com", employee_type="Full-time", designation="Annotator", status="active", slack_user_id="U_DAN3")
    db.add_all([pm_emp, lead_emp, worker])
    db.flush()

    proj = Project(name="Project Omega 3", client="TestClient", project_type="Annotation", total_tasks=100, estimated_time_per_task=1.0, start_date=date(2026, 1, 1), project_status="active", assigned_employee_ids=[pm_emp.id])
    db.add(proj)
    db.flush()

    lead_alloc = Allocation(employee_id=lead_emp.id, sub_project_id=proj.id, total_daily_hours=8, role_tags=["Team Lead"], is_active=True)
    worker_alloc = Allocation(employee_id=worker.id, sub_project_id=proj.id, total_daily_hours=8, is_active=True)
    db.add_all([lead_alloc, worker_alloc])
    db.commit()

    leader_notifications = []
    employee_notifications = []
    monkeypatch.setattr("app.services.slack_service.notify_project_leader_allocation_removed", lambda **kwargs: leader_notifications.append(kwargs))
    monkeypatch.setattr("app.services.slack_service.notify_employee_allocation_removed", lambda **kwargs: employee_notifications.append(kwargs))

    alloc_info = {"id": worker_alloc.id, "employee_id": worker.id, "sub_project_id": proj.id, "total_daily_hours": 8, "role_tags": []}

    # Lead removes worker
    send_unallocation_notifications(
        db=db,
        alloc_info=alloc_info,
        project_id=proj.id,
        actor_user_role="team_lead",
        actor_employee_id=lead_emp.id,
        actor_name="Lead Laura",
    )

    # ONLY PM must receive leader removal notification
    assert len(leader_notifications) == 1
    assert leader_notifications[0]["leader_slack_user_id"] == "U_PM_PETE3"
    assert leader_notifications[0]["remover_role"] == "Team Lead"

    # Employee must receive employee removal notification
    assert len(employee_notifications) == 1
    assert employee_notifications[0]["employee_slack_user_id"] == "U_DAN3"


