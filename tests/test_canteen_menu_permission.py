import pytest
from unittest.mock import MagicMock
from fastapi import HTTPException
from app.models.user import User
from app.api.company_settings import (
    require_canteen_manager,
    get_canteen_authorized_emails,
)
from app.api.auth import build_user_response


def test_canteen_authorized_emails_default():
    emails = get_canteen_authorized_emails(db=None)
    assert "jadhavashish061@gmail.com" in emails


def test_require_canteen_manager_admin_and_hr():
    db = MagicMock()
    admin = User(id=1, email="admin@autonex.com", role="admin")
    hr = User(id=2, email="hr@autonex.com", role="hr")

    assert require_canteen_manager(db=db, current_user=admin) == admin
    assert require_canteen_manager(db=db, current_user=hr) == hr


def test_require_canteen_manager_ashish():
    db = MagicMock()
    ashish = User(id=232, email="jadhavashish061@gmail.com", role="employee")

    assert require_canteen_manager(db=db, current_user=ashish) == ashish


def test_require_canteen_manager_unauthorized_employee():
    db = MagicMock()
    unauthorized = User(id=999, email="random_employee@autonex.com", role="employee")

    with pytest.raises(HTTPException) as exc_info:
        require_canteen_manager(db=db, current_user=unauthorized)

    assert exc_info.value.status_code == 403
    assert "canteen menu managers" in exc_info.value.detail.lower()


def test_build_user_response_canteen_flag():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    ashish = User(id=232, name="Ashish", email="jadhavashish061@gmail.com", role="employee", employee_id=None, skills=[])
    res_ashish = build_user_response(ashish, db)
    assert res_ashish.can_manage_canteen_menu is True

    unauthorized = User(id=999, name="Other", email="other@autonex.com", role="employee", employee_id=None, skills=[])
    res_other = build_user_response(unauthorized, db)
    assert res_other.can_manage_canteen_menu is False

    admin = User(id=1, name="Admin", email="admin@autonex.com", role="admin", employee_id=None, skills=[])
    res_admin = build_user_response(admin, db)
    assert res_admin.can_manage_canteen_menu is True
