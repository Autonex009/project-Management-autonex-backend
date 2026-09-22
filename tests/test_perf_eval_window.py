import os
from cryptography.fernet import Fernet
os.environ["SALARY_KEY"] = Fernet.generate_key().decode()

import pytest
from datetime import date, datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import database
from app.db.database import Base
from app.models.employee import Employee
from app.models.payroll import Salary, PayrollRun, PayrollBonus
from app.models.perf_eval import PerfEvaluation
from app.models.user import User
from app.services.salary_crypto import encrypt_salary
from app.api.perf_evals import router as perf_evals_router
from app.api.payroll import router as payroll_router
from app.services.auth_service import get_current_user
from app.constants.perf_params import PERF_PARAM_NAMES

# Register all models for metadata creation
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
    app.include_router(perf_evals_router)
    app.include_router(payroll_router)

    current_user_state = {"user": None}

    def override_get_current_user():
        return current_user_state["user"]

    app.dependency_overrides[database.get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    db = TestingSessionLocal()
    yield TestClient(app), db, current_user_state
    db.close()
    Base.metadata.drop_all(bind=engine)


def test_submission_window_before_22nd_rejected(client_and_db):
    client, db, auth_state = client_and_db
    emp = Employee(id=1, name="Alice", email="alice@x.com", employee_type="Full-time", status="active")
    user = User(id=1, email="alice@x.com", password_hash="hash", name="Alice", role="employee", employee_id=1, is_active=True)
    auth_state["user"] = user
    db.add_all([emp, user])
    db.commit()

    # Mock IST date to 21st September 2026
    fake_now = datetime(2026, 9, 21, 14, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
    with patch("app.api.perf_evals.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        payload = {
            "project_id": 1,
            "employee_id": 1,
            "period": "2026-09",
            "parameter_values": [{"name": p, "employee_rating": 4} for p in PERF_PARAM_NAMES],
            "overall_comment": "Monthly review"
        }
        resp = client.post("/api/perf-evals", json=payload)
        assert resp.status_code == 403
        assert "submitted between the 22nd and 25th" in resp.json()["detail"]


def test_submission_window_after_25th_rejected(client_and_db):
    client, db, auth_state = client_and_db
    emp = Employee(id=1, name="Alice", email="alice@x.com", employee_type="Full-time", status="active")
    user = User(id=1, email="alice@x.com", password_hash="hash", name="Alice", role="employee", employee_id=1, is_active=True)
    auth_state["user"] = user
    db.add_all([emp, user])
    db.commit()

    # Mock IST date to 26th September 2026 (closed window)
    fake_now = datetime(2026, 9, 26, 9, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    with patch("app.api.perf_evals.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        payload = {
            "project_id": 1,
            "employee_id": 1,
            "period": "2026-09",
            "parameter_values": [{"name": p, "employee_rating": 4} for p in PERF_PARAM_NAMES],
            "overall_comment": "Late submission"
        }
        resp = client.post("/api/perf-evals", json=payload)
        assert resp.status_code == 403
        assert "closed on the 25th of the month" in resp.json()["detail"]


def test_submission_window_between_22nd_and_25th_accepted(client_and_db):
    client, db, auth_state = client_and_db
    emp = Employee(id=1, name="Alice", email="alice@x.com", employee_type="Full-time", status="active")
    user = User(id=1, email="alice@x.com", password_hash="hash", name="Alice", role="employee", employee_id=1, is_active=True)
    auth_state["user"] = user
    db.add_all([emp, user])
    db.commit()

    # Mock IST date to 23rd September 2026
    fake_now = datetime(2026, 9, 23, 11, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    with patch("app.api.perf_evals.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        payload = {
            "project_id": 1,
            "employee_id": 1,
            "period": "2026-09",
            "parameter_values": [{"name": p, "employee_rating": 4} for p in PERF_PARAM_NAMES],
            "overall_comment": "On-time review"
        }
        resp = client.post("/api/perf-evals", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "submitted"
        assert data["period"] == "2026-09"

        # Duplicate submission in the same period is rejected (409)
        resp_dup = client.post("/api/perf-evals", json=payload)
        assert resp_dup.status_code == 409


def test_payroll_bonus_eligibility_tied_to_self_evaluation(client_and_db):
    client, db, auth_state = client_and_db
    admin_user = User(id=99, email="admin@x.com", password_hash="hash", name="Admin", role="admin", is_active=True)
    auth_state["user"] = admin_user
    db.add(admin_user)

    # Two employees with monthly bonus limit in salary
    emp1 = Employee(id=1, name="Bob Submitter", email="bob@x.com", employee_type="Full-time", status="active")
    emp2 = Employee(id=2, name="Charlie Missing", email="charlie@x.com", employee_type="Full-time", status="active")
    db.add_all([emp1, emp2])

    sal1 = Salary(id=1, full_name="Bob Submitter", status="active",
                  base_pay_monthly=encrypt_salary(50000.0), opt_bonus_monthly=encrypt_salary(5000.0))
    sal2 = Salary(id=2, full_name="Charlie Missing", status="active",
                  base_pay_monthly=encrypt_salary(50000.0), opt_bonus_monthly=encrypt_salary(5000.0))
    db.add_all([sal1, sal2])

    # Only Bob submitted self-evaluation for 2026-09
    eval_bob = PerfEvaluation(
        project_id=1,
        employee_id=1,
        period="2026-09",
        parameter_values=[],
        status="submitted",
        submitted_by=1,
    )
    db.add(eval_bob)
    db.commit()

    # Preview payroll for 2026-09
    resp = client.get("/api/payroll/preview", params={"month": "2026-09"})
    assert resp.status_code == 200
    rows = {r["employee_id"]: r for r in resp.json()["employees"]}

    # Bob submitted -> bonus eligible
    assert rows[1]["bonus_eligible"] is True
    assert rows[1]["bonus_limit"] == 5000.0
    assert rows[1]["bonus_ineligible_reason"] is None

    # Charlie missed -> bonus ineligible
    assert rows[2]["bonus_eligible"] is False
    assert rows[2]["bonus"] == 0.0
    assert "Self-evaluation not completed" in rows[2]["bonus_ineligible_reason"]

    # Saving payroll ignores bonus for ineligible employee
    save_payload = {
        "month": "2026-09",
        "status": "draft",
        "adjustments": [],
        "bonuses": [
            {"employee_id": 1, "amount": 4000.0},
            {"employee_id": 2, "amount": 4000.0},  # should be skipped because Charlie is ineligible
        ],
        "additional_payments": []
    }
    save_resp = client.post("/api/payroll/save", json=save_payload)
    assert save_resp.status_code == 200

    saved_run = db.query(PayrollRun).filter(PayrollRun.month == "2026-09").first()
    assert saved_run is not None
    saved_bonuses = db.query(PayrollBonus).filter(PayrollBonus.payroll_run_id == saved_run.id).all()
    # Only Bob's bonus is persisted
    assert len(saved_bonuses) == 1
    assert saved_bonuses[0].employee_id == 1
    assert saved_bonuses[0].amount == 4000.0
