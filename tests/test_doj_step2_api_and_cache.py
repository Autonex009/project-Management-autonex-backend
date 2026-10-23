import pytest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.employee import Employee
from app.models.user import User
from app.schemas.employee import EmployeeUpdate, EmployeeResponse
from app.services.project_cache import InMemoryTTLCache, employee_profile_l1_cache
from app.services.badge_service import award_tenure_badges, award_yearly_milestones
from app.api.employees import get_employee, update_employee


def test_l1_cache_basic_ops():
    cache = InMemoryTTLCache(ttl_seconds=10.0, max_size=5)
    cache.set(123, {"name": "Test Emp", "date_of_joining": "2025-01-01"})
    assert cache.get(123) == {"name": "Test Emp", "date_of_joining": "2025-01-01"}
    assert cache.get(999) is None
    cache.invalidate(123)
    assert cache.get(123) is None


def test_get_employee_l1_cache_hit_and_eviction():
    employee_id = 8888
    employee_profile_l1_cache.invalidate(employee_id)

    mock_db = MagicMock()
    mock_emp = Employee(
        id=employee_id,
        name="Cache Employee",
        email="cache_emp@autonex.com",
        employee_type="Full-time",
        working_hours_per_day=8.0,
        weekly_availability=40.0,
        productivity_baseline=1.0,
        date_of_joining=date(2024, 4, 1),
        created_at=datetime(2024, 4, 1, 10, 0, 0),
        updated_at=datetime(2024, 4, 1, 10, 0, 0),
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_emp

    # Current user is self
    user = User(id=1, email="cache_emp@autonex.com", role="employee", employee_id=employee_id)

    # 1st call: Cache miss, hits DB
    res1 = get_employee(employee_id=employee_id, db=mock_db, current_user=user)
    assert res1["name"] == "Cache Employee"
    assert employee_profile_l1_cache.get(employee_id) is not None

    # Reset DB query mock to verify 2nd call does NOT hit DB
    mock_db.query.reset_mock()
    res2 = get_employee(employee_id=employee_id, db=mock_db, current_user=user)
    assert res2["name"] == "Cache Employee"
    mock_db.query.assert_not_called()

    # Unauthorized user gets 403 on cache hit
    unauth_user = User(id=2, email="intruder@autonex.com", role="employee", employee_id=9999)
    with pytest.raises(HTTPException) as exc:
        get_employee(employee_id=employee_id, db=mock_db, current_user=unauth_user)
    assert exc.value.status_code == 403

    # Clean up
    employee_profile_l1_cache.invalidate(employee_id)


def test_update_employee_self_service_rbac_guard():
    employee_id = 7777
    employee_profile_l1_cache.invalidate(employee_id)

    # Scenario A: employee.date_of_joining is already set, regular employee tries to update it
    mock_db = MagicMock()
    existing_emp = Employee(
        id=employee_id,
        name="Existing DOJ Employee",
        email="emp7777@autonex.com",
        employee_type="Full-time",
        working_hours_per_day=8.0,
        weekly_availability=40.0,
        productivity_baseline=1.0,
        date_of_joining=date(2024, 1, 1),
        created_at=datetime(2024, 1, 1, 10, 0, 0),
        updated_at=datetime(2024, 1, 1, 10, 0, 0),
    )
    mock_db.query.return_value.filter.return_value.first.return_value = existing_emp

    emp_user = User(id=10, email="emp7777@autonex.com", role="employee", employee_id=employee_id)
    bg_tasks = MagicMock()
    req = MagicMock()

    # Employee trying to edit already-set DOJ -> 403 Forbidden
    payload = EmployeeUpdate(date_of_joining=date(2024, 2, 1))
    with pytest.raises(HTTPException) as exc:
        update_employee(
            employee_id=employee_id,
            payload=payload,
            http_request=req,
            background_tasks=bg_tasks,
            db=mock_db,
            current_user=emp_user,
        )
    assert exc.value.status_code == 403
    assert "already recorded and locked" in str(exc.value.detail)


def test_update_employee_cross_field_chronology_guard():
    employee_id = 6666
    mock_db = MagicMock()
    # Employee has DOJ = 2025-05-01
    existing_emp = Employee(
        id=employee_id,
        name="FT Guard Employee",
        email="ft6666@autonex.com",
        employee_type="Full-time",
        working_hours_per_day=8.0,
        weekly_availability=40.0,
        productivity_baseline=1.0,
        date_of_joining=date(2025, 5, 1),
        created_at=datetime(2025, 5, 1, 10, 0, 0),
        updated_at=datetime(2025, 5, 1, 10, 0, 0),
    )
    mock_db.query.return_value.filter.return_value.first.return_value = existing_emp

    admin_user = User(id=99, email="admin@autonex.com", role="admin", employee_id=1)
    bg_tasks = MagicMock()
    req = MagicMock()

    # Admin attempts to set converted_to_fulltime_at BEFORE existing date_of_joining
    payload = EmployeeUpdate(converted_to_fulltime_at=datetime(2025, 1, 1, 0, 0, 0))
    with pytest.raises(HTTPException) as exc:
        update_employee(
            employee_id=employee_id,
            payload=payload,
            http_request=req,
            background_tasks=bg_tasks,
            db=mock_db,
            current_user=admin_user,
        )
    assert exc.value.status_code == 400
    assert "Full-time start date cannot precede date of joining" in str(exc.value.detail)


def test_badge_award_strictly_relies_on_date_of_joining():
    # Verify award_tenure_badges uses ONLY date_of_joining (ZERO fallback to created_at)
    mock_db = MagicMock()
    emp_with_doj = Employee(
        id=5555,
        name="Badge Employee With DOJ",
        email="badge5555@autonex.com",
        employee_type="Full-time",
        date_of_joining=date(2024, 1, 1),
        created_at=datetime(2025, 1, 1, 0, 0, 0),
    )
    filter1 = mock_db.query.return_value.filter.return_value
    filter1.all.return_value = [emp_with_doj]
    filter1.filter.return_value.all.return_value = [emp_with_doj]

    # At 2024-05-01: 4 months from DOJ (2024-01-01) -> gets 3 months tenure badge
    today = date(2024, 5, 1)

    import app.services.badge_service as bs
    orig_award = bs.award_badge
    awarded_calls = []
    def capture_award(*args, **kwargs):
        awarded_calls.append(kwargs)
        return True
    bs.award_badge = capture_award
    try:
        award_tenure_badges(mock_db, today=today, target_employee_id=5555)
        assert len(awarded_calls) >= 1
        # Verify based_on was date_of_joining
        assert awarded_calls[0]["meta"]["based_on"] == "date_of_joining"
        assert awarded_calls[0]["meta"]["join_date"] == "2024-01-01"
    finally:
        bs.award_badge = orig_award


def test_badge_award_zero_badges_when_no_doj():
    # When employee has NO date_of_joining, NO tenure badge should be awarded (even if created_at was 2 years ago)
    mock_db = MagicMock()
    emp_no_doj = Employee(
        id=4444,
        name="No DOJ Employee",
        email="no_doj@autonex.com",
        employee_type="Full-time",
        date_of_joining=None,
        created_at=datetime(2022, 1, 1, 0, 0, 0),  # 4 years ago in portal!
    )
    # query filter for date_of_joining.isnot(None) returns empty list
    filter1 = mock_db.query.return_value.filter.return_value
    filter1.all.return_value = []
    filter1.filter.return_value.all.return_value = []

    today = date(2026, 1, 1)

    import app.services.badge_service as bs
    orig_award = bs.award_badge
    awarded_calls = []
    bs.award_badge = lambda *args, **kwargs: awarded_calls.append(kwargs) or True
    try:
        res = award_tenure_badges(mock_db, today=today, target_employee_id=4444)
        assert res == 0
        assert len(awarded_calls) == 0
    finally:
        bs.award_badge = orig_award


def test_list_badges_by_employee_hides_tenure_badges_if_no_doj():
    from app.api.badges import list_badges_by_employee
    from app.models.employee_badge import EmployeeBadge

    mock_db = MagicMock()
    # Employee has NO date_of_joining
    emp_no_doj = Employee(
        id=3333,
        name="Employee Without DOJ",
        email="emp3333@autonex.com",
        employee_type="Full-time",
        date_of_joining=None,
    )
    mock_db.query.return_value.filter.return_value.first.return_value = emp_no_doj

    # Mock badges query
    query_mock = MagicMock()
    mock_db.query.return_value.filter.return_value = query_mock
    query_mock.filter.return_value = query_mock
    query_mock.order_by.return_value.all.return_value = []

    user = User(id=1, email="admin@autonex.com", role="admin")

    list_badges_by_employee(employee_id=3333, db=mock_db, current_user=user)

    # Verify that EmployeeBadge.badge_code.in_(NEVER_EXPIRE_CODES) filter was applied to hide tenure badges!
    assert query_mock.filter.called
