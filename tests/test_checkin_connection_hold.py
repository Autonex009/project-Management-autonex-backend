"""The check-in request must not hold a DB connection across Slack calls.

Everyone checks in within minutes of the 10:00 reminder. With the Slack
confirmation done inline, one check-in kept a pooled connection checked out for
roughly a second, so a pool of 8 served under 9 check-ins/sec and the morning
rush turned into pool timeouts. These tests pin the two properties that fix
depends on: the notification is deferred to a background task, and that task
opens no session while talking to Slack.
"""
import inspect

import app.api.checkins as checkins


def test_submit_checkin_defers_slack_to_a_background_task():
    src = inspect.getsource(checkins.submit_checkin)
    assert "_notify_checkin_on_slack" in src, "Slack notification is not scheduled"
    assert "background_tasks.add_task" in src, "notification is not deferred"

    # The blocking Slack calls must not appear in the request handler itself.
    for call in (
        "try_send_checkin_success_message",
        "update_checkin_reminder_to_completed",
        "find_today_slack_reminder",
    ):
        assert call not in src, f"{call} still runs inside the request"


def test_background_notifier_closes_its_session_before_calling_slack():
    src = inspect.getsource(checkins._notify_checkin_on_slack)

    # Look at call sites, not the import block at the top of the function, so
    # rindex rather than index.
    close_at = src.index("db.close()")
    for call in ("try_send_checkin_success_message", "find_today_slack_reminder"):
        assert src.rindex(call) > close_at, (
            f"{call} runs before the session is closed — that puts a Slack round "
            f"trip back inside the connection window"
        )


def test_background_notifier_takes_plain_values_not_orm_objects():
    """A background task runs after the session closes, so ORM instances passed
    into it would be detached and raise on attribute access."""
    params = inspect.signature(checkins._notify_checkin_on_slack).parameters
    assert list(params) == ["employee_id", "work_mode", "checked_in_str"]
    for name, p in params.items():
        assert p.annotation in (int, str), f"{name} should be a primitive, got {p.annotation}"
