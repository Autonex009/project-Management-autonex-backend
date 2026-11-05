from datetime import date
from unittest.mock import patch, MagicMock
from app.constants.leave_types import get_working_days_count
from app.services.slack_service import build_leave_request_blocks
from app.api.leaves import _bg_notify_leave_applied
from app.models.leave import Leave
from app.models.employee import Employee
from app.models.user import User


def test_get_working_days_count_spanning_weekend():
    # Nov 13, 2026 is Friday, Nov 16, 2026 is Monday.
    # Nov 14 & 15 are Saturday & Sunday.
    # Total calendar days = 4, but working days = 2.
    start = date(2026, 11, 13)
    end = date(2026, 11, 16)
    assert get_working_days_count(start, end) == 2


def test_get_working_days_count_spanning_fixed_holiday():
    # Nov 6, 2026 (Fri) to Nov 10, 2026 (Tue)
    # Nov 7 & 8 are weekend.
    # Nov 9 is Govardhan Puja (fixed holiday).
    # Working days: Nov 6 (Fri), Nov 10 (Tue) = 2 days.
    start = date(2026, 11, 6)
    end = date(2026, 11, 10)
    assert get_working_days_count(start, end) == 2


def test_get_working_days_count_half_day():
    start = date(2026, 11, 13)
    end = date(2026, 11, 13)
    assert get_working_days_count(start, end, is_half_day=True) == 0.5


def test_get_working_days_count_weekend_only():
    start = date(2026, 11, 14)  # Saturday
    end = date(2026, 11, 15)    # Sunday
    assert get_working_days_count(start, end) == 0


def test_build_leave_request_blocks_duration_format():
    text, blocks = build_leave_request_blocks(
        pm_name="Jane PM",
        employee_name="Aditya Naikwad",
        employee_email="aditya@example.com",
        employee_designation="Engineer",
        leave_type="Paid Leave",
        start_date="2026-11-13",
        end_date="2026-11-16",
        duration_days=2,
        reason="Family function",
        impacted_projects=["Project Alpha"],
        leave_id=101,
        exceeds_limit=False,
        leave_balances_text=None,
        exceeds_limit_text=None,
    )
    assert "• *Duration:* 2 Day(s)" in blocks[0]["text"]["text"]
    assert "2026-11-13 to 2026-11-16" in blocks[0]["text"]["text"]


def test_bg_notify_leave_applied_sends_working_days_duration():
    mock_session = MagicMock()
    mock_leave = MagicMock()
    mock_leave.id = 1
    mock_leave.employee_id = 10
    mock_leave.leave_type = "paid"
    mock_leave.start_date = date(2026, 11, 13)  # Friday
    mock_leave.end_date = date(2026, 11, 16)    # Monday (4 calendar days, 2 working days)
    mock_leave.is_half_day = False
    mock_leave.reason = "Personal"
    mock_leave.flagged = False

    mock_employee = MagicMock()
    mock_employee.id = 10
    mock_employee.name = "Aditya Naikwad"
    mock_employee.email = "aditya@example.com"
    mock_employee.designation = "Engineer"
    mock_employee.slack_user_id = "U12345"

    def query_side_effect(model):
        q = MagicMock()
        if model == Leave:
            q.filter.return_value.first.return_value = mock_leave
        elif model == Employee:
            q.filter.return_value.first.return_value = mock_employee
        elif model == User:
            q.filter.return_value.first.return_value = None
        else:
            q.filter.return_value.all.return_value = []
            q.filter.return_value.first.return_value = None
        return q

    mock_session.query.side_effect = query_side_effect
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_session
    mock_cm.__exit__.return_value = None

    mock_pm_employee = MagicMock()
    mock_pm_employee.id = 99
    mock_pm_employee.name = "Project Manager"

    targets = [
        {
            "pm_slack_user_id": "UPM999",
            "pm_employee": mock_pm_employee,
            "impacted_projects": ["Autonex"],
        }
    ]

    with patch("app.db.database.SessionLocal", return_value=mock_cm), \
         patch("app.api.leaves._get_pm_notification_targets", return_value=targets), \
         patch("app.api.leaves.project_scope.escalates_to_admin", return_value=False), \
         patch("app.api.leaves.project_scope.escalates_to_pm", return_value=False), \
         patch("app.api.leaves.try_send_leave_applied_message"), \
         patch("app.api.leaves.get_leave_balances_text", return_value=""), \
         patch("app.api.leaves.try_get_or_cache_employee_slack_user_id", return_value="U12345"), \
         patch("app.api.leaves.try_send_pm_leave_request_message") as mock_send_pm:

        mock_send_pm.return_value = ("ts123", "ch123")
        _bg_notify_leave_applied(leave_id=1, exceeds_by=0.0)

        mock_send_pm.assert_called_once()
        call_kwargs = mock_send_pm.call_args[1]
        # Crucial check: duration_days must be 2, NOT 4!
        assert call_kwargs["duration_days"] == 2
        assert call_kwargs["start_date"] == "2026-11-13"
        assert call_kwargs["end_date"] == "2026-11-16"
