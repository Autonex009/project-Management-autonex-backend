import pytest
from datetime import date, datetime, timedelta
from pydantic import ValidationError

from app.models.employee import Employee
from app.schemas.employee import EmployeeBase, EmployeeUpdate, EmployeeResponse, parse_and_validate_doj
from app.utils.business_time import today_ist


def test_parse_and_validate_doj_valid():
    assert parse_and_validate_doj("2024-05-15") == date(2024, 5, 15)
    assert parse_and_validate_doj("2024-05-15T00:00:00") == date(2024, 5, 15)
    assert parse_and_validate_doj(datetime(2024, 5, 15, 10, 30)) == date(2024, 5, 15)
    assert parse_and_validate_doj(date(2024, 5, 15)) == date(2024, 5, 15)


def test_parse_and_validate_doj_empty_coercion():
    assert parse_and_validate_doj(None) is None
    assert parse_and_validate_doj("") is None
    assert parse_and_validate_doj("   ") is None
    assert parse_and_validate_doj("null") is None
    assert parse_and_validate_doj("None") is None


def test_parse_and_validate_doj_rejects_future():
    future_date = (today_ist() + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="cannot be in the future"):
        parse_and_validate_doj(future_date)


def test_parse_and_validate_doj_invalid_format():
    with pytest.raises(ValueError, match="Invalid date format"):
        parse_and_validate_doj("invalid-date")


def test_employee_base_chronology_guard():
    # Valid: conversion date matches or is after DOJ
    emp = EmployeeBase(
        name="Valid Employee",
        email="valid@autonex.com",
        employee_type="Full-time",
        date_of_joining=date(2024, 1, 1),
        converted_to_fulltime_at=datetime(2024, 6, 1, 0, 0, 0),
    )
    assert emp.date_of_joining == date(2024, 1, 1)

    # Invalid: converted to full time BEFORE joining date
    with pytest.raises(ValidationError) as exc_info:
        EmployeeBase(
            name="Invalid Employee",
            email="invalid@autonex.com",
            employee_type="Full-time",
            date_of_joining=date(2024, 6, 1),
            converted_to_fulltime_at=datetime(2024, 1, 1, 0, 0, 0),
        )
    assert "Full-time start date cannot precede date of joining" in str(exc_info.value)


def test_employee_update_chronology_guard():
    # Updating both fields with an invalid order should fail
    with pytest.raises(ValidationError) as exc_info:
        EmployeeUpdate(
            date_of_joining=date(2025, 6, 1),
            converted_to_fulltime_at=datetime(2025, 1, 1, 0, 0, 0),
        )
    assert "Full-time start date cannot precede date of joining" in str(exc_info.value)


def test_employee_orm_to_response_serialization():
    emp = Employee(
        id=101,
        name="Jane Senior",
        email="jane.senior@autonex.com",
        employee_type="Full-time",
        working_hours_per_day=8.0,
        weekly_availability=40.0,
        productivity_baseline=1.0,
        date_of_joining=date(2024, 3, 10),
        created_at=datetime(2024, 3, 15, 9, 30, 0),
        updated_at=datetime(2024, 3, 15, 9, 30, 0),
        converted_to_fulltime_at=datetime(2024, 6, 10, 0, 0, 0),
    )
    resp = EmployeeResponse.model_validate(emp)
    assert resp.date_of_joining == date(2024, 3, 10)
    assert resp.created_at == datetime(2024, 3, 15, 9, 30, 0)
    assert resp.converted_to_fulltime_at == datetime(2024, 6, 10, 0, 0, 0)
