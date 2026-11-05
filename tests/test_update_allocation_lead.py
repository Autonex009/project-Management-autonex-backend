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
import app.models.onboarding_pipeline  # noqa: F401

from app.api.allocations import router as allocations_router
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
    api.include_router(allocations_router)
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
        role_tags=["Annotation"],
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
        "allocation": allocation,
        "project": project,
        "employee": employee,
    }


def test_update_allocation_role_tags_does_not_crash_on_is_active(env):
    client = env["client"]
    alloc_id = env["allocation"].id

    # This mimics frontend syncTeamLeadAllocations calling allocationApi.update(held.id, { role_tags: [...] })
    response = client.put(f"/api/allocations/{alloc_id}", json={
        "role_tags": ["Annotation", "Team Lead"],
    })

    assert response.status_code == 200, response.text
    data = response.json()
    assert "Team Lead" in data["role_tags"]


def test_update_allocation_can_update_is_active(env):
    client = env["client"]
    alloc_id = env["allocation"].id

    # Deactivate
    response = client.put(f"/api/allocations/{alloc_id}", json={
        "is_active": False,
        "deactivated_reason": "Testing deactivation",
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["is_active"] is False
    assert data["deactivated_reason"] == "Testing deactivation"

    # Reactivate
    response = client.put(f"/api/allocations/{alloc_id}", json={
        "is_active": True,
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["is_active"] is True
    assert data["deactivated_reason"] is None
