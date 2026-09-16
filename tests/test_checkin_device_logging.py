"""Tests for checkin device logging (desktop vs phone)."""
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
import app.models.daily_checkin    # noqa: F401
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
from app.models.daily_checkin import DailyCheckIn
from app.models.project import Project
from app.models.allocation import Allocation


from app.api.checkins import router as checkins_router, detect_device_type, normalize_device_type
from app.services.auth_service import get_current_user


def test_detect_device_type_user_agents():
    # iPhone
    iphone_ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"
    assert detect_device_type(iphone_ua) == "mobile"

    # Android Mobile
    android_mobile_ua = "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36"
    assert detect_device_type(android_mobile_ua) == "mobile"

    # iPad
    ipad_ua = "Mozilla/5.0 (iPad; CPU OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"
    assert detect_device_type(ipad_ua) == "tablet"

    # Desktop Mac Chrome
    desktop_mac_ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    assert detect_device_type(desktop_mac_ua) == "desktop"

    # Desktop Windows Chrome
    desktop_win_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    assert detect_device_type(desktop_win_ua) == "desktop"

    # None / empty
    assert detect_device_type(None) == "desktop"
    assert detect_device_type("") == "desktop"


def test_normalize_device_type():
    assert normalize_device_type("phone") == "mobile"
    assert normalize_device_type("Mobile") == "mobile"
    assert normalize_device_type("desktop") == "desktop"
    assert normalize_device_type("Tablet") == "tablet"
    # Fallback to User-Agent if payload is empty/None
    iphone_ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5) Mobile/15E148 Safari/604.1"
    assert normalize_device_type(None, user_agent=iphone_ua) == "mobile"


from sqlalchemy import event, func

@event.listens_for(DailyCheckIn, "before_insert")
def set_daily_checkin_id(mapper, connection, target):
    if target.id is None:
        max_id = connection.scalar(func.coalesce(func.max(DailyCheckIn.id), 0)) or 0
        target.id = max_id + 1


@pytest.fixture()
def ctx():
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
    emp = Employee(name="Checkin Tester", email="test@autonexai360.com", employee_type="Full-time",
                   designation="Developer", status="active", skills=[])
    db.add(emp)
    db.flush()

    from datetime import date
    proj = Project(
        name="Project A",
        client="Client A",
        project_type="annotation",
        total_tasks=10,
        estimated_time_per_task=1.0,
        start_date=date.today(),
    )
    db.add(proj)
    db.flush()

    alloc = Allocation(employee_id=emp.id, sub_project_id=proj.id, is_active=True)
    db.add(alloc)

    login = User(id=1, name="Checkin Tester", email="test@autonexai360.com", password_hash="hash",
                 role="admin", employee_id=emp.id, is_active=True, skills=[])
    db.add(login)
    db.commit()

    state = {"user": login, "db": db}

    app = FastAPI()
    app.include_router(checkins_router)
    app.dependency_overrides[database.get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: state["user"]

    client = TestClient(app)
    return client, state, proj.id, emp.id


def test_checkin_logs_mobile_from_user_agent(ctx):
    client, state, proj_id, emp_id = ctx
    iphone_ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.5 Mobile/15E148 Safari/604.1"

    payload = {
        "work_mode": "WFH",
        "project_ids": [proj_id],
        "mood": "great",
    }
    resp = client.post("/api/checkins", json=payload, headers={"User-Agent": iphone_ua})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["device_type"] == "mobile"

    # Verify in admin paginated checkins
    resp_admin = client.get("/api/checkins/admin/paginated")
    assert resp_admin.status_code == 200, resp_admin.text
    admin_data = resp_admin.json()
    matching = [item for item in admin_data["items"] if item["employee_id"] == emp_id]
    assert len(matching) == 1
    assert matching[0]["device_type"] == "mobile"
    assert matching[0]["checked_in"] is True


def test_checkin_logs_explicit_device_type(ctx):
    client, state, proj_id, emp_id = ctx

    payload = {
        "work_mode": "WFO",
        "office_floor": "7",
        "lunch_preference": "none",
        "project_ids": [proj_id],
        "mood": "okay",
        "device_type": "desktop",
    }
    resp = client.post("/api/checkins", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["device_type"] == "desktop"

    # Verify in admin paginated checkins
    resp_admin = client.get("/api/checkins/admin/paginated")
    assert resp_admin.status_code == 200, resp_admin.text
    admin_data = resp_admin.json()
    matching = [item for item in admin_data["items"] if item["employee_id"] == emp_id]
    assert len(matching) == 1
    assert matching[0]["device_type"] == "desktop"
