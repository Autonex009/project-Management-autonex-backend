"""Whoever creates a project is put on it immediately.

Being the recorded manager (``assigned_employee_ids``) is not enough on its own: the
Allocations page and the manpower avatar strip are built from **allocation rows**, so
without one the person who just created the project is missing from both screens.

The three cases that differ:

* a **program manager** creating one  → manager seat + an allocation
* a **team lead** creating one        → an allocation *tagged* as the lead, and NO manager
  seat, since that seat would give them rank over the project's other leads
* an **admin** creating one           → neither; admins create on other people's behalf and
  are not staff on the project
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

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
import app.models.audit_log        # noqa: F401
import app.models.employee         # noqa: F401
import app.models.guideline        # noqa: F401
import app.models.leave            # noqa: F401
import app.models.notification     # noqa: F401
import app.models.parent_project   # noqa: F401
import app.models.payroll          # noqa: F401
import app.models.perf_eval        # noqa: F401
import app.models.project          # noqa: F401
import app.models.referral         # noqa: F401
import app.models.side_project     # noqa: F401
import app.models.signup_request   # noqa: F401
import app.models.skill            # noqa: F401
import app.models.sub_project      # noqa: F401
import app.models.user             # noqa: F401
import app.models.wfh              # noqa: F401

from app.api.projects import router as projects_router
from app.models.allocation import Allocation
from app.models.employee import Employee
from app.models.project import DailySheet
from app.models.user import User
from app.services.auth_service import get_current_user

_CALLER = {}


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    api = FastAPI()
    api.include_router(projects_router)
    api.dependency_overrides[database.get_db] = override_get_db
    api.dependency_overrides[get_current_user] = lambda: _CALLER["user"]

    db = TestingSessionLocal()
    people = {}
    for key, designation, role in (
        ("pm", "Program Manager", "pm"),
        ("lead", "Team Lead", "team_lead"),
        ("admin", "Admin", "admin"),
    ):
        employee = Employee(name=key.title(), email=f"{key}@x.com", status="active",
                            employee_type="Full-time", designation=designation)
        db.add(employee)
        db.commit()
        db.refresh(employee)
        user = User(email=employee.email, password_hash="x", name=employee.name,
                    role=role, employee_id=employee.id, is_active=True)
        db.add(user)
        db.commit()
        db.refresh(user)
        people[key] = {"employee": employee, "user": user}

    yield TestClient(api), db, people
    db.close()
    Base.metadata.drop_all(bind=engine)


def _create(client, name="New project"):
    return client.post(
        "/api/sub-projects",
        json={
            "name": name,
            "client": "Acme",
            "project_type": "Full",
            "total_tasks": 10,
            "estimated_time_per_task": 0.5,
            "start_date": date(2026, 4, 1).isoformat(),
            "project_duration_weeks": 4,
            "project_duration_days": 28,
            "autonex_annotators": 1,
            "autonex_reviewers": 1,
        },
    )


def _allocations_for(db, project_id, employee_id):
    return (
        db.query(Allocation)
        .filter(
            Allocation.sub_project_id == project_id,
            Allocation.employee_id == employee_id,
        )
        .all()
    )


def test_a_pm_who_creates_a_project_is_allocated_to_it(env):
    client, db, people = env
    pm = people["pm"]
    _CALLER["user"] = pm["user"]

    resp = _create(client, "PM project")
    assert resp.status_code == 200, resp.text
    project_id = resp.json()["id"]

    rows = _allocations_for(db, project_id, pm["employee"].id)
    assert len(rows) == 1, "the creator must appear on the Allocations page"
    # Overridden so an existing full workload can never block creating a project.
    assert rows[0].override_flag is True
    assert rows[0].override_reason

    # ...and still recorded as the project's manager.
    project = db.query(DailySheet).filter(DailySheet.id == project_id).first()
    assert pm["employee"].id in (project.assigned_employee_ids or [])


def test_a_team_lead_who_creates_a_project_is_its_lead_not_its_manager(env):
    """The seat and the tag are different things, and the distinction is the hierarchy."""
    client, db, people = env
    lead = people["lead"]
    _CALLER["user"] = lead["user"]

    resp = _create(client, "Lead project")
    assert resp.status_code == 200, resp.text
    project_id = resp.json()["id"]

    rows = _allocations_for(db, project_id, lead["employee"].id)
    assert len(rows) == 1
    assert "Team Lead" in (rows[0].role_tags or []), "tagged as this project's lead"

    project = db.query(DailySheet).filter(DailySheet.id == project_id).first()
    assert lead["employee"].id not in (project.assigned_employee_ids or []), (
        "a lead must not take the manager seat — it would outrank the other leads"
    )


def test_an_admin_who_creates_a_project_is_not_allocated(env):
    """Admins create on other people's behalf, so they are not staff on the project."""
    client, db, people = env
    admin = people["admin"]
    _CALLER["user"] = admin["user"]

    resp = _create(client, "Admin project")
    assert resp.status_code == 200, resp.text
    project_id = resp.json()["id"]

    assert _allocations_for(db, project_id, admin["employee"].id) == []


def test_required_manpower_excludes_qc(env):
    """QC is no longer part of a project's team composition.

    Sending it must not inflate the requirement — the column survives for historical rows,
    but nothing adds to it.
    """
    client, db, people = env
    _CALLER["user"] = people["pm"]["user"]

    resp = client.post(
        "/api/sub-projects",
        json={
            "name": "No QC",
            "client": "Acme",
            "project_type": "Full",
            "total_tasks": 5,
            "estimated_time_per_task": 0.5,
            "start_date": date(2026, 4, 1).isoformat(),
            "project_duration_weeks": 4,
            "project_duration_days": 28,
            "autonex_annotators": 2,
            "autonex_reviewers": 1,
            "qc_count": 7,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["required_manpower"] == 3, "annotators + reviewers only"
