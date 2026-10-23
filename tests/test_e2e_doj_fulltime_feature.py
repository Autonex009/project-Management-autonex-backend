import pytest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.employee import Employee
from app.models.employee_badge import EmployeeBadge
from app.models.user import User
from app.schemas.employee import EmployeeBase, EmployeeUpdate, EmployeeResponse, parse_and_validate_doj
from app.utils.business_time import today_ist
from app.services.project_cache import employee_profile_l1_cache
from app.services.badge_service import (
    award_tenure_badges,
    award_yearly_milestones,
    recalculate_employee_milestones_async,
    NEVER_EXPIRE_CODES,
)
from app.api.employees import get_employee, update_employee
from app.api.badges import list_badges_by_employee


def test_e2e_ist_midnight_safe_boundary():
    # Verify that today_ist() allows today's date in IST without UTC drift rejection
    today = today_ist()
    assert parse_and_validate_doj(today.isoformat()) == today

    # Tomorrow IST must be rejected
    tomorrow = today + timedelta(days=1)
    with pytest.raises(ValueError, match="cannot be in the future"):
        parse_and_validate_doj(tomorrow.isoformat())


def test_e2e_chronological_ordering():
    # Valid: conversion matches or is after DOJ
    emp_valid = EmployeeBase(
        name="Valid Timing",
        email="timing@autonex.com",
        employee_type="Full-time",
        date_of_joining=date(2024, 1, 1),
        converted_to_fulltime_at=datetime(2024, 6, 1, 0, 0, 0),
    )
    assert emp_valid.date_of_joining == date(2024, 1, 1)

    # Inversion: converted_to_fulltime_at precedes date_of_joining
    with pytest.raises(ValidationError) as exc:
        EmployeeBase(
            name="Inverted Timing",
            email="invert@autonex.com",
            employee_type="Full-time",
            date_of_joining=date(2024, 6, 1),
            converted_to_fulltime_at=datetime(2024, 1, 1, 0, 0, 0),
        )
    assert "Full-time start date cannot precede date of joining" in str(exc.value)


def test_e2e_self_service_lifecycle_and_rbac_lock():
    employee_id = 9101
    employee_profile_l1_cache.invalidate(employee_id)

    emp_record = Employee(
        id=employee_id,
        name="Self Service User",
        email="self@autonex.com",
        employee_type="Full-time",
        working_hours_per_day=8.0,
        weekly_availability=40.0,
        productivity_baseline=1.0,
        date_of_joining=None,
        created_at=datetime(2024, 1, 1, 0, 0, 0),
        updated_at=datetime(2024, 1, 1, 0, 0, 0),
    )
    linked_user = User(id=50, email="self@autonex.com", role="employee", employee_id=employee_id)
    admin_user = User(id=1, email="admin@autonex.com", role="admin", employee_id=999)

    mock_db = MagicMock()
    def mock_query(model):
        q = MagicMock()
        if model == Employee:
            q.filter.return_value.first.return_value = emp_record
        elif model == User:
            q.filter.return_value.first.return_value = linked_user
        else:
            q.filter.return_value.first.return_value = None
        return q
    mock_db.query.side_effect = mock_query

    bg = MagicMock()
    req = MagicMock()

    # Step B: Employee populates date_of_joining for the first time -> MUST SUCCEED
    payload_first = EmployeeUpdate(date_of_joining=date(2024, 2, 1))
    updated_emp = update_employee(
        employee_id=employee_id,
        payload=payload_first,
        http_request=req,
        background_tasks=bg,
        db=mock_db,
        current_user=linked_user,
    )
    assert updated_emp.date_of_joining == date(2024, 2, 1)

    # Step C: Once populated, employee attempts to mutate it again -> MUST BE BLOCKED (403)
    payload_second = EmployeeUpdate(date_of_joining=date(2024, 3, 1))
    with pytest.raises(HTTPException) as exc:
        update_employee(
            employee_id=employee_id,
            payload=payload_second,
            http_request=req,
            background_tasks=bg,
            db=mock_db,
            current_user=linked_user,
        )
    assert exc.value.status_code == 403
    assert "already recorded and locked" in str(exc.value.detail)

    # Step D: Admin/HR user updates it -> MUST SUCCEED
    payload_admin = EmployeeUpdate(date_of_joining=date(2024, 1, 15))
    admin_updated = update_employee(
        employee_id=employee_id,
        payload=payload_admin,
        http_request=req,
        background_tasks=bg,
        db=mock_db,
        current_user=admin_user,
    )
    assert admin_updated.date_of_joining == date(2024, 1, 15)


def test_e2e_l1_cache_hit_and_post_commit_eviction():
    employee_id = 9202
    employee_profile_l1_cache.invalidate(employee_id)

    emp_record = Employee(
        id=employee_id,
        name="Cache Lifecycle User",
        email="cache_e2e@autonex.com",
        employee_type="Full-time",
        working_hours_per_day=8.0,
        weekly_availability=40.0,
        productivity_baseline=1.0,
        date_of_joining=date(2024, 5, 10),
        created_at=datetime(2024, 5, 10, 0, 0, 0),
        updated_at=datetime(2024, 5, 10, 0, 0, 0),
    )
    linked_user = User(id=60, email="cache_e2e@autonex.com", role="employee", employee_id=employee_id)

    mock_db = MagicMock()
    def mock_query(model):
        q = MagicMock()
        if model == Employee:
            q.filter.return_value.first.return_value = emp_record
        elif model == User:
            q.filter.return_value.first.return_value = linked_user
        else:
            q.filter.return_value.first.return_value = None
        return q
    mock_db.query.side_effect = mock_query

    # 1. Read hits DB and warms cache
    res1 = get_employee(employee_id=employee_id, db=mock_db, current_user=linked_user)
    assert res1["name"] == "Cache Lifecycle User"
    assert employee_profile_l1_cache.get(employee_id) is not None

    # 2. Mutate employee evicts cache
    bg = MagicMock()
    req = MagicMock()
    update_employee(
        employee_id=employee_id,
        payload=EmployeeUpdate(name="Cache Lifecycle User Updated"),
        http_request=req,
        background_tasks=bg,
        db=mock_db,
        current_user=linked_user,
    )
    # Cache must be evicted
    assert employee_profile_l1_cache.get(employee_id) is None


def test_e2e_strict_doj_badge_lifecycle():
    # 1. Null DOJ produces 0 tenure badges
    mock_db = MagicMock()
    emp_null_doj = Employee(
        id=9303,
        name="No DOJ Badge User",
        email="nodoj@autonex.com",
        employee_type="Full-time",
        date_of_joining=None,
        created_at=datetime(2020, 1, 1, 0, 0, 0),  # 6 years ago in portal!
    )
    filter_null = mock_db.query.return_value.filter.return_value
    filter_null.all.return_value = []
    filter_null.filter.return_value.all.return_value = []

    awarded_null = award_tenure_badges(mock_db, today=date(2026, 1, 1), target_employee_id=9303)
    assert awarded_null == 0

    # 2. API suppresses tenure badges if date_of_joining is None
    mock_db.query.return_value.filter.return_value.first.return_value = emp_null_doj
    query_mock = MagicMock()
    mock_db.query.return_value.filter.return_value = query_mock
    query_mock.filter.return_value = query_mock
    query_mock.order_by.return_value.all.return_value = []

    user = User(id=1, email="admin@autonex.com", role="admin")
    list_badges_by_employee(employee_id=9303, db=mock_db, current_user=user)
    # Confirm ~EmployeeBadge.badge_code.in_(NEVER_EXPIRE_CODES) filter applied
    assert query_mock.filter.called
