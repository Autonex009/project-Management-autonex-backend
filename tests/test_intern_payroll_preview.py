"""
Verification (end-to-end): intern payroll preview
==================================================
An intern with two paid leaves in the same month: the first is PAID (no salary
impact), the second is auto-classified UNPAID (one day deducted). The reported
paid balance is monthly.
"""
import os
os.environ.pop("SLACK_BOT_TOKEN", None)
# Salary is encrypted at rest; the payroll path needs the key to read it.
from cryptography.fernet import Fernet
os.environ["SALARY_KEY"] = Fernet.generate_key().decode()

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

from app.models.employee import Employee
from app.models.leave import Leave
from app.api.payroll import router as payroll_router


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
    app.include_router(payroll_router)
    app.dependency_overrides[database.get_db] = override_get_db
    db = TestingSessionLocal()
    yield TestClient(app), db
    db.close()
    Base.metadata.drop_all(bind=engine)


def test_intern_second_monthly_paid_leave_is_deducted(client_and_db):
    client, db = client_and_db
    from app.services.salary_crypto import encrypt_salary
    intern = Employee(name="Ira Intern", email="ira@x.com", employee_type="Intern",
                      status="active", base_salary_enc=encrypt_salary(22000.0))
    db.add(intern)
    db.commit()
    db.refresh(intern)

    # Two single-day PAID leaves in Jan 2026 (both weekdays, not holidays)
    db.add_all([
        Leave(employee_id=intern.id, leave_type="paid",
              start_date=date(2026, 1, 5), end_date=date(2026, 1, 5), status="approved"),
        Leave(employee_id=intern.id, leave_type="paid",
              start_date=date(2026, 1, 6), end_date=date(2026, 1, 6), status="approved"),
    ])
    db.commit()

    resp = client.get("/api/payroll/preview", params={"month": "2026-01"})
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json()["employees"] if r["employee_id"] == intern.id)

    # 2 leave days this month: 1 paid, 1 unpaid (the second), 1 day deducted.
    assert row["total_leave_days"] == 2
    assert row["total_paid_days"] == 1
    assert row["total_deducted_days"] == 1
    assert row["total_deduction"] == row["per_day_rate"]            # exactly one day docked
    assert row["final_salary"] == round(22000.0 - row["per_day_rate"], 2)

    # Monthly paid balance: 1/month, the paid day consumed it.
    paid_bal = row["leave_balances"]["paid"]
    assert paid_bal["period"] == "month"
    assert paid_bal["quota"] == 1 and paid_bal["remaining"] == 0
