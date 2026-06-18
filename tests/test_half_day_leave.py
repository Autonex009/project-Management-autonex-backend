import os
os.environ.pop("SLACK_BOT_TOKEN", None)
os.environ.pop("RAZORPAY_API_ID", None)
os.environ.pop("RAZORPAY_API_KEY", None)

from cryptography.fernet import Fernet
os.environ["SALARY_KEY"] = Fernet.generate_key().decode()

import sys
from datetime import date, datetime, timezone, timedelta
from unittest.mock import patch

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
from app.models.user import User
from app.api.leaves import router as leave_router
from app.api.payroll import router as payroll_router
from app.services.salary_crypto import encrypt_salary

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
    app.include_router(payroll_router)
    app.dependency_overrides[database.get_db] = override_get_db

    db = TestingSessionLocal()
    yield TestClient(app), db
    db.close()
    Base.metadata.drop_all(bind=engine)


def _seed_employee(db):
    emp = Employee(name="Karan Dev", email="karan@x.com", status="active",
                   employee_type="employee", base_salary_enc=encrypt_salary(30000.0),
                   razorpay_email="karan@x.com")
    db.add(emp)
    db.commit()
    db.refresh(emp)
    
    # Create admin user to approve leaves and access payroll
    admin = User(email="admin@x.com", password_hash="x", name="Admin Boss", role="admin", is_active=True)
    db.add(admin)
    db.commit()
    db.refresh(admin)
    
    return emp, admin


def test_create_half_day_leave_validation_errors(client_and_db):
    client, db = client_and_db
    emp, admin = _seed_employee(db)

    # Error: start_date != end_date for a half-day leave
    payload = {
        "employee_id": emp.id,
        "leave_type": "paid",
        "start_date": "2026-06-22",
        "end_date": "2026-06-23",
        "is_half_day": True,
        "half_day_slot": "first_half",
        "reason": "Doctor visit"
    }
    resp = client.post("/api/leaves", json=payload)
    assert resp.status_code == 422
    assert "end_date must be equal to start_date for half-day leaves" in resp.text

    # Error: missing slot for half-day
    payload["end_date"] = "2026-06-22"
    payload["half_day_slot"] = None
    resp = client.post("/api/leaves", json=payload)
    assert resp.status_code == 422
    assert "half_day_slot must be 'first_half' or 'second_half' when is_half_day is True" in resp.text

    # Error: invalid slot value
    payload["half_day_slot"] = "evening_half"
    resp = client.post("/api/leaves", json=payload)
    assert resp.status_code == 422


def test_first_half_leave_timing_rules(client_and_db):
    client, db = client_and_db
    emp, admin = _seed_employee(db)

    # India time is mocked to 2026-06-16 10:00:00 IST
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    mocked_dt = datetime(2026, 6, 16, 10, 0, tzinfo=ist_tz)
    
    with patch("app.api.leaves.get_current_ist_datetime", return_value=mocked_dt):
        # 1. Apply for tomorrow (2026-06-17) first_half -> Should SUCCEED (at least 1 day advance)
        payload = {
            "employee_id": emp.id,
            "leave_type": "first_half",
            "start_date": "2026-06-17",
            "end_date": "2026-06-17",
            "reason": "Appointment"
        }
        resp = client.post("/api/leaves", json=payload)
        assert resp.status_code == 201

        # 2. Apply for today (2026-06-16) first_half -> Should FAIL
        payload = {
            "employee_id": emp.id,
            "leave_type": "first_half",
            "start_date": "2026-06-16",
            "end_date": "2026-06-16",
            "reason": "Appointment"
        }
        resp = client.post("/api/leaves", json=payload)
        assert resp.status_code == 400
        assert "First-half leaves must be applied at least one day in advance" in resp.json()["detail"]


def test_second_half_leave_timing_rules(client_and_db):
    client, db = client_and_db
    emp, admin = _seed_employee(db)

    ist_tz = timezone(timedelta(hours=5, minutes=30))

    # Test 1: Apply for today before 2:00 PM IST (e.g. 1:59 PM) -> Should SUCCEED
    mocked_dt_before = datetime(2026, 6, 16, 13, 59, tzinfo=ist_tz)
    with patch("app.api.leaves.get_current_ist_datetime", return_value=mocked_dt_before):
        payload = {
            "employee_id": emp.id,
            "leave_type": "second_half",
            "start_date": "2026-06-16",
            "end_date": "2026-06-16",
            "reason": "Personal work"
        }
        resp = client.post("/api/leaves", json=payload)
        assert resp.status_code == 201

    # Clear leaves to allow next tests on same date
    db.query(Leave).delete()
    db.commit()

    # Test 2: Apply for today after 2:00 PM IST (e.g. 2:01 PM) -> Should FAIL
    mocked_dt_after = datetime(2026, 6, 16, 14, 1, tzinfo=ist_tz)
    with patch("app.api.leaves.get_current_ist_datetime", return_value=mocked_dt_after):
        payload = {
            "employee_id": emp.id,
            "leave_type": "second_half",
            "start_date": "2026-06-16",
            "end_date": "2026-06-16",
            "reason": "Personal work"
        }
        resp = client.post("/api/leaves", json=payload)
        assert resp.status_code == 400
        assert "Second-half leaves must be applied before 2:00 PM on the same day" in resp.json()["detail"]


def test_leave_overlaps_logic(client_and_db):
    client, db = client_and_db
    emp, admin = _seed_employee(db)

    # Seed an approved half-day leave on 2026-06-22 (Monday)
    db.add(Leave(employee_id=emp.id, leave_type="first_half",
                 start_date=date(2026, 6, 22), end_date=date(2026, 6, 22),
                 status="approved", is_half_day=True, half_day_slot="first_half"))
    db.commit()

    # Reject trying to apply for another half-day leave on same date
    payload = {
        "employee_id": emp.id,
        "leave_type": "second_half",
        "start_date": "2026-06-22",
        "end_date": "2026-06-22",
        "reason": "Overlap test"
    }
    resp = client.post("/api/leaves", json=payload)
    assert resp.status_code == 409
    assert "A leave already exists for this period" in resp.json()["detail"]

    # Reject trying to apply for a full-day leave overlapping same date
    payload = {
        "employee_id": emp.id,
        "leave_type": "paid",
        "start_date": "2026-06-19",
        "end_date": "2026-06-23",
        "is_half_day": False,
        "reason": "Overlap test full"
    }
    resp = client.post("/api/leaves", json=payload)
    assert resp.status_code == 409


def test_payroll_preview_and_direct_unpaid_calculation(client_and_db):
    client, db = client_and_db
    emp, admin = _seed_employee(db)

    # Add 1 approved half-day leave in Jan 2026 (Jan 5 is a Monday)
    db.add(Leave(employee_id=emp.id, leave_type="first_half",
                 start_date=date(2026, 1, 5), end_date=date(2026, 1, 5),
                 status="approved", is_half_day=True, half_day_slot="first_half"))
    db.commit()

    # Access preview payroll endpoint
    resp = client.get("/api/payroll/preview", params={"month": "2026-01"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    row = next(r for r in data["employees"] if r["employee_id"] == emp.id)

    # Verify 0.5 deducted days, rate split, and unpaid status
    assert row["total_leave_days"] == 0.5
    assert row["total_paid_days"] == 0.0
    assert row["total_deducted_days"] == 0.5
    assert row["payable_days"] == data["working_days"] - 0.5
    
    # Deductions should equal exactly half a day's salary rate
    expected_deduction = round(0.5 * row["per_day_rate"], 2)
    assert row["total_deduction"] == expected_deduction
    assert row["final_salary"] == round(30000.0 - expected_deduction, 2)

    # Check that balances were NOT impacted (i.e. Paid balance is still full)
    paid_bal = row["leave_balances"]["paid"]
    assert paid_bal["quota"] == 12
    assert paid_bal["used"] == 0
    assert paid_bal["remaining"] == 12


def test_half_day_bypasses_razorpay_sync(client_and_db):
    client, db = client_and_db
    emp, admin = _seed_employee(db)

    # Create an approved half-day leave
    leave = Leave(employee_id=emp.id, leave_type="first_half",
                  start_date=date(2026, 6, 25), end_date=date(2026, 6, 25),
                  status="approved", is_half_day=True, half_day_slot="first_half")
    db.add(leave)
    db.commit()

    # Manual apply to Razorpay should raise 400 bad request error for half-day leaves
    resp = client.post(f"/api/leaves/{leave.id}/apply-to-razorpay")
    assert resp.status_code == 400
    assert "Half-day leaves do not sync to Razorpay" in resp.json()["detail"]


def test_consecutive_leaves_blocking_basic(client_and_db):
    client, db = client_and_db
    emp, admin = _seed_employee(db)

    # 4 consecutive working days (Monday 2026-06-22 to Thursday 2026-06-25) -> Should FAIL
    payload = {
        "employee_id": emp.id,
        "leave_type": "paid",
        "start_date": "2026-06-22",
        "end_date": "2026-06-25",
        "reason": "4 consecutive days"
    }
    resp = client.post("/api/leaves", json=payload)
    assert resp.status_code == 400
    assert "Safe guard triggered" in resp.json()["detail"]


def test_consecutive_leaves_across_weekend_split(client_and_db):
    client, db = client_and_db
    emp, admin = _seed_employee(db)

    # Apply for Friday (2026-06-19) -> Should SUCCEED
    db.add(Leave(employee_id=emp.id, leave_type="paid",
                 start_date=date(2026, 6, 19), end_date=date(2026, 6, 19),
                 status="approved"))
    db.commit()

    # Now apply for Monday (2026-06-22) to Wednesday (2026-06-24) -> Should FAIL
    # (Friday + Mon + Tue + Wed = 4 consecutive working days, bridging Sat/Sun)
    payload = {
        "employee_id": emp.id,
        "leave_type": "paid",
        "start_date": "2026-06-22",
        "end_date": "2026-06-24",
        "reason": "Split bridging weekend"
    }
    resp = client.post("/api/leaves", json=payload)
    assert resp.status_code == 400
    assert "Safe guard triggered" in resp.json()["detail"]


def test_consecutive_leaves_half_days_ignored(client_and_db):
    client, db = client_and_db
    emp, admin = _seed_employee(db)

    # India time is mocked to 2026-06-16 10:00:00 IST
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    mocked_dt = datetime(2026, 6, 16, 10, 0, tzinfo=ist_tz)
    
    with patch("app.api.leaves.get_current_ist_datetime", return_value=mocked_dt):
        # 1. Existing half-day on Friday (2026-06-19)
        db.add(Leave(employee_id=emp.id, leave_type="first_half",
                     start_date=date(2026, 6, 19), end_date=date(2026, 6, 19),
                     status="approved", is_half_day=True, half_day_slot="first_half"))
        db.commit()

        # Apply for Monday (2026-06-22) to Wednesday (2026-06-24) (3 days)
        # Friday is half-day, so it shouldn't bridge/count towards the 4-day block.
        # Total full days = 3. Should SUCCEED.
        payload = {
            "employee_id": emp.id,
            "leave_type": "paid",
            "start_date": "2026-06-22",
            "end_date": "2026-06-24",
            "reason": "Full leaves after half-day"
        }
        resp = client.post("/api/leaves", json=payload)
        assert resp.status_code == 201

        # Clear leaves for next check
        db.query(Leave).delete()
        db.commit()

        # 2. Applying for a half-day itself should always succeed, even with adjacent leaves.
        # Thursday (2026-06-18) full, Monday (2026-06-22) full, Tuesday (2026-06-23) full.
        db.add(Leave(employee_id=emp.id, leave_type="paid",
                     start_date=date(2026, 6, 18), end_date=date(2026, 6, 18),
                     status="approved"))
        db.add(Leave(employee_id=emp.id, leave_type="paid",
                     start_date=date(2026, 6, 22), end_date=date(2026, 6, 23),
                     status="approved"))
        db.commit()

        # Request Friday (2026-06-19) as first_half -> Should SUCCEED (even though adjacent working days are leave)
        payload_half = {
            "employee_id": emp.id,
            "leave_type": "first_half",
            "start_date": "2026-06-19",
            "end_date": "2026-06-19",
            "reason": "Half-day in between"
        }
        resp = client.post("/api/leaves", json=payload_half)
        assert resp.status_code == 201


def test_consecutive_leaves_fixed_holiday_ignored(client_and_db):
    client, db = client_and_db
    emp, admin = _seed_employee(db)

    # Muharram is June 26, 2026 (Friday) - a fixed holiday.
    # Apply for Tuesday (2026-06-23) to Thursday (2026-06-25) -> 3 days.
    db.add(Leave(employee_id=emp.id, leave_type="paid",
                 start_date=date(2026, 6, 23), end_date=date(2026, 6, 25),
                 status="approved"))
    db.commit()

    # Now apply for Monday (2026-06-29) -> 1 day.
    # Tuesday-Thursday (3 days) + Friday (Holiday, skipped) + Sat/Sun (Weekend, skipped) + Monday (1 day)
    # Total consecutive = 4 days -> Should FAIL
    payload = {
        "employee_id": emp.id,
        "leave_type": "paid",
        "start_date": "2026-06-29",
        "end_date": "2026-06-29",
        "reason": "Split bridging weekend and holiday"
    }
    resp = client.post("/api/leaves", json=payload)
    assert resp.status_code == 400
    assert "Safe guard triggered" in resp.json()["detail"]

