from unittest.mock import patch
from app.services.slack_service import (
    get_slack_user_info,
    lookup_user_avatar_by_email,
    lookup_user_avatar_by_user_id,
    try_lookup_employee_avatar,
)


class DummyEmployee:
    def __init__(self, email=None, slack_user_id=None):
        self.email = email
        self.slack_user_id = slack_user_id


def test_lookup_user_avatar_by_user_id_success():
    mock_response = {
        "ok": True,
        "user": {
            "profile": {
                "image_512": "https://avatars.slack-edge.com/user_512.jpg",
                "image_original": "https://avatars.slack-edge.com/user_original.jpg",
            }
        },
    }
    with patch("app.services.slack_service._slack_request", return_value=mock_response):
        avatar = lookup_user_avatar_by_user_id("U0B5VND9BNC")
        assert avatar == "https://avatars.slack-edge.com/user_original.jpg"


def test_try_lookup_employee_avatar_prefers_slack_user_id():
    emp = DummyEmployee(email="personal@gmail.com", slack_user_id="U0B5VND9BNC")
    with patch(
        "app.services.slack_service.lookup_user_avatar_by_user_id",
        return_value="https://avatars.slack-edge.com/avatar_id.jpg",
    ) as mock_user_id_lookup, patch(
        "app.services.slack_service.lookup_user_avatar_by_email"
    ) as mock_email_lookup:
        avatar = try_lookup_employee_avatar(emp)
        assert avatar == "https://avatars.slack-edge.com/avatar_id.jpg"
        mock_user_id_lookup.assert_called_once_with("U0B5VND9BNC")
        mock_email_lookup.assert_not_called()


def test_try_lookup_employee_avatar_falls_back_to_email():
    emp = DummyEmployee(email="work@company.com", slack_user_id=None)
    with patch(
        "app.services.slack_service.lookup_user_avatar_by_email",
        return_value="https://avatars.slack-edge.com/avatar_email.jpg",
    ) as mock_email_lookup:
        avatar = try_lookup_employee_avatar(emp)
        assert avatar == "https://avatars.slack-edge.com/avatar_email.jpg"
        mock_email_lookup.assert_called_once_with("work@company.com")


def test_get_slack_user_info_fetches_profile_by_id_only():
    mock_payload = {
        "ok": True,
        "user": {
            "id": "U0B5VND9BNC",
            "real_name": "Siddharth Kitake",
            "profile": {
                "email": "siddharth.k@autonexai360.com",
                "image_512": "https://avatars.slack-edge.com/512.jpg",
            },
        },
    }
    with patch("app.services.slack_service._slack_request", return_value=mock_payload):
        user_info = get_slack_user_info("U0B5VND9BNC")
        assert user_info["id"] == "U0B5VND9BNC"
        assert user_info["profile"]["email"] == "siddharth.k@autonexai360.com"

