import os
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import date
import sys
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tests.conftest  # Sets up SQLite composite primary key fix
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
import app.models.onboarding_pipeline  # noqa: F401

from app.api.employees import router as employees_router
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
    api.include_router(employees_router)
    api.dependency_overrides[database.get_db] = override_get_db
    api.dependency_overrides[get_current_user] = lambda: _CALLER["user"]

    db = TestingSessionLocal()
    admin_emp = Employee(name="Admin", email="admin@x.com", status="active", employee_type="Full-time", designation="Admin")
    db.add(admin_emp)
    db.commit()
    db.refresh(admin_emp)

    admin_user = User(email=admin_emp.email, password_hash="x", name="Admin", role="admin", employee_id=admin_emp.id, is_active=True)
    db.add(admin_user)
    db.commit()
    db.refresh(admin_user)

    employee = Employee(name="Worker", email="worker@x.com", status="active", employee_type="Full-time", designation="Annotator")
    db.add(employee)
    db.commit()
    db.refresh(employee)

    project = DailySheet(
        name="Dense Caption Project",
        client="TestClient",
        total_tasks=10,
        estimated_time_per_task=0.5,
        start_date=date(2026, 4, 1),
        project_duration_weeks=4,
        project_duration_days=28,
        project_status="active",
        project_type="Production",
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    allocation = Allocation(
        employee_id=employee.id,
        sub_project_id=project.id,
        total_daily_hours=8,
        role_tags=[],
        is_active=True,
    )
    db.add(allocation)
    db.commit()
    db.refresh(allocation)

    _CALLER["user"] = admin_user
    client = TestClient(api)

    return {
        "client": client,
        "db": db,
        "employee": employee,
        "project": project,
        "allocation": allocation,
    }


def test_designation_change_to_team_lead_syncs_leadership(env):
    client = env["client"]
    emp = env["employee"]
    db = env["db"]
    project = env["project"]

    # 1. Update designation to "Team Lead"
    res = client.put(f"/api/employees/{emp.id}", json={"designation": "Team Lead"})
    assert res.status_code == 200

    db.expire_all()
    updated_proj = db.query(DailySheet).filter(DailySheet.id == project.id).first()
    updated_alloc = db.query(Allocation).filter(Allocation.id == env["allocation"].id).first()

    assert updated_proj.team_lead_count == 1
    assert "Team Lead" in (updated_alloc.role_tags or [])


def test_designation_change_from_team_lead_to_pm_syncs_leadership(env):
    client = env["client"]
    emp = env["employee"]
    db = env["db"]
    project = env["project"]

    # First set to Team Lead
    client.put(f"/api/employees/{emp.id}", json={"designation": "Team Lead"})

    # Now change to Project Manager
    res = client.put(f"/api/employees/{emp.id}", json={"designation": "Project Manager"})
    assert res.status_code == 200

    db.expire_all()
    updated_proj = db.query(DailySheet).filter(DailySheet.id == project.id).first()
    updated_alloc = db.query(Allocation).filter(Allocation.id == env["allocation"].id).first()

    assert updated_proj.team_lead_count == 0
    assert "Team Lead" not in (updated_alloc.role_tags or [])
    assert emp.id in (updated_proj.assigned_employee_ids or [])
    assert updated_proj.team_manager_count == 1


def test_designation_change_from_pm_to_annotator_syncs_leadership(env):
    client = env["client"]
    emp = env["employee"]
    db = env["db"]
    project = env["project"]

    # First set to Project Manager
    client.put(f"/api/employees/{emp.id}", json={"designation": "Project Manager"})

    # Now demote back to Annotator
    res = client.put(f"/api/employees/{emp.id}", json={"designation": "Annotator"})
    assert res.status_code == 200

    db.expire_all()
    updated_proj = db.query(DailySheet).filter(DailySheet.id == project.id).first()
    updated_alloc = db.query(Allocation).filter(Allocation.id == env["allocation"].id).first()

    assert updated_proj.team_lead_count == 0
    assert emp.id not in (updated_proj.assigned_employee_ids or [])
    assert updated_proj.team_manager_count == 0
