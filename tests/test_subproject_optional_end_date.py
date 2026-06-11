"""
Verification: sub-project End Date is optional
==============================================
The create form labels End Date "(optional)". This verifies the backend
honours that: a sub-project can be created with no end_date, and the capacity
engine reports a neutral "no_end_date" status instead of crashing on date math.
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

from app.models.parent_project import MainProject
from app.api.projects import router as projects_router
from app.services.recommendation_service import RecommendationEngine


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
    app.include_router(projects_router)
    app.dependency_overrides[database.get_db] = override_get_db
    db = TestingSessionLocal()
    yield TestClient(app), db
    db.close()
    Base.metadata.drop_all(bind=engine)


def _seed_parent(db):
    mp = MainProject(name="Parent", project_type="Full", client="Acme",
                     global_start_date=date(2026, 1, 1), status="active")
    db.add(mp)
    db.commit()
    db.refresh(mp)
    return mp


def test_create_sub_project_without_end_date(client_and_db):
    client, db = client_and_db
    mp = _seed_parent(db)

    resp = client.post("/api/sub-projects", json={
        "name": "Open-ended sheet",
        "main_project_id": mp.id,
        "total_tasks": 100,
        "estimated_time_per_task": 0.5,
        "required_expertise": [],
        "assigned_employee_ids": [],
        "start_date": "2026-07-01",
        "end_date": None,          # <-- omitted in the UI
        "daily_target": 10,
        "project_duration_weeks": 0,
        "project_duration_days": 0,
        "required_manpower": 2,
        "project_status": "active",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["end_date"] is None
    assert body["start_date"] == "2026-07-01"
    assert body["id"] > 0

    # And it shows up in the list endpoint without 500-ing on the null column
    listed = client.get("/api/sub-projects")
    assert listed.status_code == 200, listed.text
    assert any(p["id"] == body["id"] and p["end_date"] is None for p in listed.json())


def test_capacity_engine_handles_null_end_date(client_and_db):
    client, db = client_and_db
    mp = _seed_parent(db)
    resp = client.post("/api/sub-projects", json={
        "name": "Open-ended sheet 2",
        "main_project_id": mp.id,
        "total_tasks": 50,
        "estimated_time_per_task": 1.0,
        "required_expertise": [],
        "start_date": "2026-07-01",
        "end_date": None,
        "project_duration_weeks": 0,
        "project_duration_days": 0,
    })
    sub_id = resp.json()["id"]

    result = RecommendationEngine(db).calculate_project_capacity(sub_id)
    assert "error" not in result
    assert result["status"] == "no_end_date"
    assert result["total_estimated_hours"] == 50.0  # 50 tasks * 1.0 hr
