from datetime import date, datetime, timezone, timedelta
from app.constants.leave_types import (
    compute_prorated_paid_leave_quota,
    get_leave_quota,
    INTERN_MONTHLY_PAID_QUOTA,
)


def test_intern_and_contractor_always_monthly():
    # Interns always return monthly quota (1) regardless of joining/conversion date
    assert compute_prorated_paid_leave_quota(2026, "Intern", date_of_joining=date(2026, 1, 1)) == 1
    assert compute_prorated_paid_leave_quota(2026, "Contractor", date_of_joining=date(2026, 7, 1)) == 1
    assert compute_prorated_paid_leave_quota(2026, "contract", date_of_joining=date(2026, 11, 1)) == 1


def test_converted_to_fulltime_november_kisan_jena():
    # Converted in November 2026 -> 12 - 11 + 1 = 2 leaves for remainder of 2026
    nov_conv = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
    quota = compute_prorated_paid_leave_quota(
        target_year=2026,
        employee_type="Full-time",
        converted_to_fulltime_at=nov_conv,
        date_of_joining=date(2026, 3, 1), # joined as intern in March
        created_at=datetime(2026, 2, 15, tzinfo=timezone.utc),
    )
    # Must use converted_to_fulltime_at (Nov), NOT date_of_joining (March)
    assert quota == 2


def test_older_employee_joined_in_july_current_year():
    # Direct hire employee joining in July 2026 -> 12 - 7 + 1 = 6 leaves
    quota = compute_prorated_paid_leave_quota(
        target_year=2026,
        employee_type="Full-time",
        converted_to_fulltime_at=None,
        date_of_joining=date(2026, 7, 15),
    )
    assert quota == 6


def test_pre_existing_employee_multi_year_safety():
    # Employee joined in July 2024 or 2025. In 2026, they get full 12 leaves!
    quota_2024 = compute_prorated_paid_leave_quota(
        target_year=2026,
        employee_type="Full-time",
        date_of_joining=date(2024, 7, 1),
    )
    assert quota_2024 == 12

    quota_2025 = compute_prorated_paid_leave_quota(
        target_year=2026,
        employee_type="Full-time",
        converted_to_fulltime_at=datetime(2025, 11, 1, tzinfo=timezone.utc),
    )
    assert quota_2025 == 12


def test_created_at_fallback_when_doj_is_none():
    # Employee created_at in August 2026 (accepted from signup request)
    # date_of_joining is None
    quota = compute_prorated_paid_leave_quota(
        target_year=2026,
        employee_type="Full-time",
        converted_to_fulltime_at=None,
        date_of_joining=None,
        created_at=datetime(2026, 8, 10, 9, 30, tzinfo=timezone.utc),
    )
    # August (8) -> 12 - 8 + 1 = 5 leaves
    assert quota == 5


def test_timezone_midnight_shift():
    # Oct 31, 2026 19:30 UTC = Nov 1, 2026 01:00 IST (+5:30)
    # Must be recognized as November in IST -> 2 leaves, NOT October -> 3 leaves
    utc_oct_end = datetime(2026, 10, 31, 19, 30, tzinfo=timezone.utc)
    quota = compute_prorated_paid_leave_quota(
        target_year=2026,
        employee_type="Full-time",
        converted_to_fulltime_at=utc_oct_end,
    )
    assert quota == 2


def test_future_employee():
    # Employee starting in 2027 evaluated in 2026 -> 0 leaves
    quota = compute_prorated_paid_leave_quota(
        target_year=2026,
        employee_type="Full-time",
        date_of_joining=date(2027, 2, 1),
    )
    assert quota == 0


def test_string_dates_robustness():
    # String date ISO formatted
    quota = compute_prorated_paid_leave_quota(
        target_year=2026,
        employee_type="Full-time",
        date_of_joining="2026-07-20T00:00:00",
    )
    assert quota == 6


def test_get_leave_quota_unified():
    # Default without employee info -> 12 for paid, 2 for floater, 6 for casual_sick
    assert get_leave_quota("paid") == 12
    assert get_leave_quota("floater") == 2
    assert get_leave_quota("casual_sick") == 6
    # Intern casual_sick is 0
    assert get_leave_quota("casual_sick", employee_type="Intern") == 0
    # Custom paid quota
    assert get_leave_quota("paid", custom_paid_quota=2) == 2
    # Prorated via get_leave_quota directly
    assert get_leave_quota("paid", target_year=2026, employee_type="Full-time", converted_to_fulltime_at=datetime(2026, 11, 1, tzinfo=timezone.utc)) == 2
    assert get_leave_quota("paid", target_year=2026, employee_type="Full-time", date_of_joining=date(2026, 7, 1)) == 6


class _MockLeave:
    def __init__(self, id, start, end, leave_type="paid", status="approved"):
        self.id = id
        self.start_date = start
        self.end_date = end
        self.leave_type = leave_type
        self.status = status


def test_classify_year_leaves_with_prorated_quota_november_kisan_jena():
    from app.api.payroll import _classify_year_leaves

    # Kisan Jena converted in Nov -> paid quota = 2
    # Weekdays in 2026: Nov 10 (Tue), Dec 1 (Tue), Dec 2 (Wed)
    leaves = [
        _MockLeave(101, date(2026, 11, 10), date(2026, 11, 10)),  # Nov 1st leave -> Paid
        _MockLeave(102, date(2026, 12, 1), date(2026, 12, 1)),    # Dec 2nd leave -> Paid
        _MockLeave(103, date(2026, 12, 2), date(2026, 12, 2)),    # Dec 3rd leave -> Excess -> UNPAID!
    ]
    classification, balances = _classify_year_leaves(
        leaves,
        year=2026,
        intern=False,
        paid_quota_override=2,
    )

    assert balances["paid"]["quota"] == 2
    assert balances["paid"]["used"] == 3.0
    assert balances["paid"]["remaining"] == 0

    # Leave 101: Paid
    assert len(classification[101]["paid_dates"]) == 1
    assert len(classification[101]["unpaid_dates"]) == 0

    # Leave 102: Paid
    assert len(classification[102]["paid_dates"]) == 1
    assert len(classification[102]["unpaid_dates"]) == 0

    # Leave 103: Exceeds prorated quota of 2 -> Unpaid!
    assert len(classification[103]["paid_dates"]) == 0
    assert len(classification[103]["unpaid_dates"]) == 1


def test_classify_year_leaves_promoted_intern_preserves_past_leaves():
    from app.api.payroll import _classify_year_leaves

    # Promoted on Nov 1, 2026.
    # Weekdays: Jan 5 (Mon), Feb 2 (Mon), Nov 10 (Tue), Dec 1 (Tue)
    leaves = [
        _MockLeave(201, date(2026, 1, 5), date(2026, 1, 5)),    # Intern Jan -> Paid
        _MockLeave(202, date(2026, 2, 2), date(2026, 2, 2)),    # Intern Feb -> Paid
        _MockLeave(203, date(2026, 11, 10), date(2026, 11, 10)), # FT Nov -> Paid
        _MockLeave(204, date(2026, 12, 1), date(2026, 12, 1)),   # FT Dec -> Paid
    ]
    classification, balances = _classify_year_leaves(
        leaves,
        year=2026,
        intern=False,
        intern_until=date(2026, 11, 1),
        paid_quota_override=2,
    )

    # Full-time quota is 2
    assert balances["paid"]["quota"] == 2
    # Only FT leaves (Nov & Dec) consume the FT quota
    assert balances["paid"]["used"] == 2.0
    assert balances["paid"]["remaining"] == 0

    # All 4 leaves are paid (no wrongful deductions)
    for lid in (201, 202, 203, 204):
        assert len(classification[lid]["paid_dates"]) == 1
        assert len(classification[lid]["unpaid_dates"]) == 0


class _MockQuery:
    def __init__(self, data):
        self._data = data

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self._data


class _MockDB:
    def __init__(self, leaves):
        self._leaves = leaves

    def query(self, *args, **kwargs):
        return _MockQuery(self._leaves)


class _MockEmp:
    def __init__(self, id, employee_type, converted_to_fulltime_at=None, date_of_joining=None, created_at=None):
        self.id = id
        self.employee_type = employee_type
        self.converted_to_fulltime_at = converted_to_fulltime_at
        self.date_of_joining = date_of_joining
        self.created_at = created_at


def test_slack_leave_balances_text_prorated_november_kisan_jena():
    from app.services.slack_service import get_leave_balances_text

    emp = _MockEmp(
        id=99,
        employee_type="Full-time",
        converted_to_fulltime_at=datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc),
        date_of_joining=date(2026, 3, 1),
    )
    # 1 leave used in November
    leave = _MockLeave(301, date(2026, 11, 10), date(2026, 11, 10), "paid")
    db = _MockDB([leave])

    text = get_leave_balances_text(db, emp)
    assert "• *Paid Leave:* 1 / 2 days left (1 used in 2026)" in text
    assert "• *Floater Leave:* 2 / 2 days left (0 used in 2026)" in text


def test_slack_leave_balances_text_july_joiner():
    from app.services.slack_service import get_leave_balances_text

    emp = _MockEmp(
        id=100,
        employee_type="Full-time",
        date_of_joining=date(2026, 7, 15),
    )
    db = _MockDB([])

    text = get_leave_balances_text(db, emp)
    assert "• *Paid Leave:* 6 / 6 days left (0 used in 2026)" in text


