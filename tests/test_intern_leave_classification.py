"""
Verification: intern paid-leave entitlement (1 paid leave/month, resets monthly)
================================================================================
Interns accrue PAID leave monthly: the first paid working-day in a calendar
month is PAID; further paid days that month are UNPAID; the allowance resets at
the start of each month. casual_sick / floater and ALL employee behaviour stay
on the existing annual-quota logic.

Tests the payroll classifier _classify_year_leaves(leaves, year, intern).
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.payroll import _classify_year_leaves


class _Leave:
    def __init__(self, id, start, end, leave_type, status="approved"):
        self.id = id
        self.start_date = start
        self.end_date = end
        self.leave_type = leave_type
        self.status = status


def _paid(cls, leave_id):
    return cls[leave_id]["paid_dates"]


def _unpaid(cls, leave_id):
    return cls[leave_id]["unpaid_dates"]


def test_intern_first_paid_rest_unpaid_and_monthly_reset():
    # Three single-day paid leaves in Jan 2026 (all weekdays, not holidays) + one in Feb.
    leaves = [
        _Leave(1, date(2026, 1, 5), date(2026, 1, 5), "paid"),   # Mon
        _Leave(2, date(2026, 1, 6), date(2026, 1, 6), "paid"),   # Tue
        _Leave(3, date(2026, 1, 7), date(2026, 1, 7), "paid"),   # Wed
        _Leave(4, date(2026, 2, 2), date(2026, 2, 2), "paid"),   # Mon (new month)
    ]
    cls, balances = _classify_year_leaves(leaves, 2026, intern=True)

    # First leave of January -> PAID
    assert _paid(cls, 1) == {date(2026, 1, 5)} and not _unpaid(cls, 1)
    # Additional January leaves -> UNPAID
    assert not _paid(cls, 2) and _unpaid(cls, 2) == {date(2026, 1, 6)}
    assert not _paid(cls, 3) and _unpaid(cls, 3) == {date(2026, 1, 7)}
    # February resets the allowance -> PAID again
    assert _paid(cls, 4) == {date(2026, 2, 2)} and not _unpaid(cls, 4)

    assert balances["paid"]["period"] == "month"
    assert balances["paid"]["quota"] == 1


def test_intern_multiday_paid_leave_splits_first_day_paid():
    # A single 3-working-day paid leave: only the first working day is paid.
    leaves = [_Leave(10, date(2026, 3, 9), date(2026, 3, 11), "paid")]  # Mon–Wed
    cls, _ = _classify_year_leaves(leaves, 2026, intern=True)
    assert _paid(cls, 10) == {date(2026, 3, 9)}
    assert _unpaid(cls, 10) == {date(2026, 3, 10), date(2026, 3, 11)}


def test_intern_casual_sick_still_annual():
    # casual_sick keeps the annual quota (6) for interns — two same-month days both paid.
    leaves = [
        _Leave(20, date(2026, 4, 6), date(2026, 4, 6), "casual_sick"),
        _Leave(21, date(2026, 4, 7), date(2026, 4, 7), "casual_sick"),
    ]
    cls, balances = _classify_year_leaves(leaves, 2026, intern=True)
    assert _paid(cls, 20) and _paid(cls, 21)            # both paid (within annual 6)
    assert not _unpaid(cls, 20) and not _unpaid(cls, 21)
    assert balances["casual_sick"]["period"] == "year"


def test_employee_paid_is_annual_not_monthly():
    # Employees are unaffected: two paid leaves in the same month are BOTH paid
    # (annual quota 12), unlike interns.
    leaves = [
        _Leave(30, date(2026, 1, 5), date(2026, 1, 5), "paid"),
        _Leave(31, date(2026, 1, 6), date(2026, 1, 6), "paid"),
    ]
    cls, balances = _classify_year_leaves(leaves, 2026, intern=False)
    assert _paid(cls, 30) and not _unpaid(cls, 30)
    assert _paid(cls, 31) and not _unpaid(cls, 31)
    assert balances["paid"]["quota"] == 12
    assert balances["paid"]["period"] == "year"


def test_employee_paid_exceeds_annual_quota_becomes_unpaid():
    # 13 separate single-day paid leaves across the year: first 12 paid, 13th unpaid.
    # Use the first weekday of 13 distinct months? Only 12 months — use 13 weekdays.
    days = [
        date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8),
        date(2026, 1, 9), date(2026, 1, 12), date(2026, 1, 13), date(2026, 1, 14),
        date(2026, 1, 15), date(2026, 1, 16), date(2026, 1, 19), date(2026, 1, 20),
        date(2026, 1, 21),  # 13th
    ]
    leaves = [_Leave(40 + i, d, d, "paid") for i, d in enumerate(days)]
    cls, balances = _classify_year_leaves(leaves, 2026, intern=False)
    paid_total = sum(len(c["paid_dates"]) for c in cls.values())
    unpaid_total = sum(len(c["unpaid_dates"]) for c in cls.values())
    assert paid_total == 12
    assert unpaid_total == 1
    assert balances["paid"]["remaining"] == 0
