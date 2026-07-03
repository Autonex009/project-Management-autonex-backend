import pytest
from datetime import date, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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

from app.db.database import Base
from app.models.employee import Employee
from app.models.allocation import Allocation
from app.services.allocation_validator import check_double_booking, get_employee_allocation_status

@pytest.fixture(scope="module")
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(engine)

@pytest.fixture(scope="module")
def employee(db):
    emp = Employee(
        name="John Doe",
        email="john.doe@example.com",
        employee_type="Full-time",
        designation="Annotator",
        working_hours_per_day=8.0,
        weekly_availability=40.0,
        status="active",
    )
    db.add(emp)
    db.commit()
    db.refresh(emp)
    return emp

def test_get_employee_allocation_status_ignores_past_and_future(db, employee):
    # Setup: Create a past allocation and a future allocation
    past_start = date.today() - timedelta(days=30)
    past_end = date.today() - timedelta(days=10)
    future_start = date.today() + timedelta(days=10)
    future_end = date.today() + timedelta(days=30)

    alloc_past = Allocation(
        employee_id=employee.id,
        sub_project_id=1,
        total_daily_hours=8,
        active_start_date=past_start,
        active_end_date=past_end
    )
    alloc_future = Allocation(
        employee_id=employee.id,
        sub_project_id=2,
        total_daily_hours=8,
        active_start_date=future_start,
        active_end_date=future_end
    )
    db.add(alloc_past)
    db.add(alloc_future)
    db.commit()

    # Verify: status for today should be "unallocated" with 0 hours
    status = get_employee_allocation_status(db, employee.id)
    assert status is not None
    assert status["status"] == "unallocated"
    assert status["total_allocated"] == 0

    # Cleanup
    db.delete(alloc_past)
    db.delete(alloc_future)
    db.commit()

def test_check_double_booking_handles_open_ended_correctly(db, employee):
    # Setup: Create a past allocation (8 hours)
    past_start = date.today() - timedelta(days=30)
    past_end = date.today() - timedelta(days=10)

    alloc_past = Allocation(
        employee_id=employee.id,
        sub_project_id=1,
        total_daily_hours=8,
        active_start_date=past_start,
        active_end_date=past_end
    )
    db.add(alloc_past)
    db.commit()

    # Verify: validating a new open-ended allocation starting today (8 hours)
    # should NOT conflict with the past allocation because their date ranges do not overlap.
    booking_check = check_double_booking(
        db=db,
        employee_id=employee.id,
        new_hours=8,
        active_start=date.today(),
        active_end=None
    )
    assert booking_check["is_overbooked"] is False
    assert booking_check["existing_hours"] == 0

    # Setup: Create a future allocation (8 hours)
    future_start = date.today() + timedelta(days=10)
    future_end = date.today() + timedelta(days=30)
    alloc_future = Allocation(
        employee_id=employee.id,
        sub_project_id=2,
        total_daily_hours=8,
        active_start_date=future_start,
        active_end_date=future_end
    )
    db.add(alloc_future)
    db.commit()

    # Verify: validating a new open-ended allocation starting today (8 hours)
    # should conflict with the future allocation because the open-ended allocation
    # continues indefinitely and covers the future period.
    booking_check_future = check_double_booking(
        db=db,
        employee_id=employee.id,
        new_hours=8,
        active_start=date.today(),
        active_end=None
    )
    assert booking_check_future["is_overbooked"] is True
    assert booking_check_future["existing_hours"] == 8

    # Setup: Now add a current open-ended allocation (6 hours)
    alloc_current_open = Allocation(
        employee_id=employee.id,
        sub_project_id=3,
        total_daily_hours=6,
        active_start_date=date.today(),
        active_end_date=None
    )
    db.add(alloc_current_open)
    db.commit()

    # Verify: a second open-ended allocation of 4 hours starting tomorrow
    # should overlap with the existing current open-ended allocation, causing an overbooking (6 + 4 = 10 > 8).
    booking_check_2 = check_double_booking(
        db=db,
        employee_id=employee.id,
        new_hours=4,
        active_start=date.today() + timedelta(days=1),
        active_end=None
    )
    assert booking_check_2["is_overbooked"] is True
    # The existing hours that overlap tomorrow include:
    # - alloc_current_open (6 hours)
    # - alloc_future (8 hours)
    # Total = 14 hours
    assert booking_check_2["existing_hours"] == 14

    # Cleanup
    db.delete(alloc_past)
    db.delete(alloc_future)
    db.delete(alloc_current_open)
    db.commit()
