import pytest
import app.models.sub_project  # noqa: F401
from app.api.checkins import _is_office_ip, _get_client_ip
from unittest.mock import MagicMock

def test_office_ip_matching():
    # Authorized office IPs:
    # 38.20.140.122, 103.54.189.22, 27.0.150.66
    assert _is_office_ip("38.20.140.122") is True
    assert _is_office_ip("103.54.189.22") is True
    assert _is_office_ip("27.0.150.66") is True

    # Unauthorized / external IPs
    assert _is_office_ip("192.168.1.50") is False
    assert _is_office_ip("49.36.120.45") is False
    assert _is_office_ip("8.8.8.8") is False
    assert _is_office_ip("") is False
    assert _is_office_ip(None) is False

def test_get_client_ip_headers():
    # 1. Cloudflare header
    req_cf = MagicMock()
    req_cf.headers = {"cf-connecting-ip": "38.20.140.122", "x-forwarded-for": "1.1.1.1"}
    assert _get_client_ip(req_cf) == "38.20.140.122"

    # 2. X-Real-IP header
    req_real = MagicMock()
    req_real.headers = {"x-real-ip": "103.54.189.22"}
    assert _get_client_ip(req_real) == "103.54.189.22"

    # 3. X-Forwarded-For header
    req_xff = MagicMock()
    req_xff.headers = {"x-forwarded-for": "27.0.150.66, 10.0.0.1"}
    assert _get_client_ip(req_xff) == "27.0.150.66"

    # 4. Direct socket
    req_direct = MagicMock()
    req_direct.headers = {}
    req_direct.client.host = "38.20.140.122"
    assert _get_client_ip(req_direct) == "38.20.140.122"

def test_submit_checkin_wfo_blocks_non_office_ip():
    from app.api.checkins import submit_checkin
    from app.schemas.checkin import CheckInCreate
    from fastapi import HTTPException

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None  # No existing checkin
    mock_user = MagicMock(employee_id=101)

    # Mock non-office IP
    req = MagicMock()
    req.headers = {"x-forwarded-for": "198.51.100.99"}

    payload_wfo = CheckInCreate(work_mode="WFO", project_ids=[1], mood="great")

    with pytest.raises(HTTPException) as exc_info:
        submit_checkin(payload_wfo, req, mock_db, mock_user)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Please connect to the office Wi-Fi or disconnect VPN service."

def test_submit_checkin_wfh_allows_non_office_ip():
    from app.api.checkins import submit_checkin
    from app.schemas.checkin import CheckInCreate

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None  # No existing checkin
    mock_user = MagicMock(employee_id=101)

    # Mock non-office IP
    req = MagicMock()
    req.headers = {"x-forwarded-for": "198.51.100.99"}

    payload_wfh = CheckInCreate(work_mode="WFH", project_ids=[1], mood="great")

    # Should not raise HTTPException regarding Wi-Fi / IP
    res = submit_checkin(payload_wfh, req, mock_db, mock_user)
    assert res.work_mode == "WFH"
    assert mock_db.add.called
    assert mock_db.commit.called


def test_slack_confirmation_token_roundtrip():
    from app.services.auth_service import (
        create_checkin_confirmation_token,
        decode_checkin_confirmation_token,
    )

    token = create_checkin_confirmation_token(
        employee_id=123,
        portal_ip="38.20.140.122",
        work_mode="WFO",
        project_ids=[1, 2],
        mood="great",
        checkin_date="2026-09-09",
        expires_minutes=5,
    )
    payload = decode_checkin_confirmation_token(token)
    assert payload["employee_id"] == 123
    assert payload["portal_ip"] == "38.20.140.122"
    assert payload["work_mode"] == "WFO"
    assert payload["project_ids"] == [1, 2]
    assert payload["purpose"] == "checkin_confirmation"


def test_confirm_slack_checkin_ip_matching():
    from app.api.checkins import confirm_slack_checkin
    from app.schemas.checkin import SlackConfirmRequest
    from app.services.auth_service import create_checkin_confirmation_token
    from fastapi import HTTPException

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    mock_user = MagicMock(employee_id=123)

    token = create_checkin_confirmation_token(
        employee_id=123,
        portal_ip="38.20.140.122",
        work_mode="WFO",
        project_ids=[1],
        mood="great",
        checkin_date="2026-09-09",
    )

    # 1. Matching IP (38.20.140.122 == 38.20.140.122) -> SUCCESS
    req_match = MagicMock()
    req_match.headers = {"x-forwarded-for": "38.20.140.122"}
    res = confirm_slack_checkin(SlackConfirmRequest(token=token), req_match, mock_db, mock_user)
    assert res.employee_id == 123
    assert res.work_mode == "WFO"
    assert mock_db.add.called

    # 2. Mismatched IP with fresh token -> BLOCKED (400)
    fresh_token = create_checkin_confirmation_token(
        employee_id=123,
        portal_ip="38.20.140.122",
        work_mode="WFO",
        project_ids=[1],
        mood="great",
        checkin_date="2026-09-09",
    )
    req_mismatch = MagicMock()
    req_mismatch.headers = {"x-forwarded-for": "49.36.12.10"}
    with pytest.raises(HTTPException) as exc_info:
        confirm_slack_checkin(SlackConfirmRequest(token=fresh_token), req_mismatch, mock_db, mock_user)

    assert exc_info.value.status_code == 400
    assert "IP mismatch" in exc_info.value.detail


def test_confirm_slack_checkin_single_use_burn():
    """Verify that a confirmation token cannot be reused once attempted."""
    from app.api.checkins import confirm_slack_checkin
    from app.schemas.checkin import SlackConfirmRequest
    from app.services.auth_service import create_checkin_confirmation_token
    from fastapi import HTTPException

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    mock_user = MagicMock(employee_id=123)

    token = create_checkin_confirmation_token(
        employee_id=123,
        portal_ip="38.20.140.122",
        work_mode="WFO",
        project_ids=[1],
        mood="great",
        checkin_date="2026-09-09",
    )

    req = MagicMock()
    req.headers = {"x-forwarded-for": "38.20.140.122"}
    # First attempt: succeeds and burns the token
    confirm_slack_checkin(SlackConfirmRequest(token=token), req, mock_db, mock_user)

    # Second attempt with the same token: MUST fail as already burned
    with pytest.raises(HTTPException) as exc_info:
        confirm_slack_checkin(SlackConfirmRequest(token=token), req, mock_db, mock_user)

    assert exc_info.value.status_code == 400
    assert "already been used or invalidated" in exc_info.value.detail


def test_confirm_slack_checkin_account_mismatch_403():
    """Verify that opening the link while logged in as a different user is rejected with 403."""
    from app.api.checkins import confirm_slack_checkin
    from app.schemas.checkin import SlackConfirmRequest
    from app.services.auth_service import create_checkin_confirmation_token
    from fastapi import HTTPException

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    # User B is logged in
    colleague_user = MagicMock(employee_id=999)

    # Token was issued for Employee 123
    token = create_checkin_confirmation_token(
        employee_id=123,
        portal_ip="38.20.140.122",
        work_mode="WFO",
        project_ids=[1],
        mood="great",
        checkin_date="2026-09-09",
    )

    req = MagicMock()
    req.headers = {"x-forwarded-for": "38.20.140.122"}
    with pytest.raises(HTTPException) as exc_info:
        confirm_slack_checkin(SlackConfirmRequest(token=token), req, mock_db, colleague_user)

    assert exc_info.value.status_code == 403
    assert "Account mismatch" in exc_info.value.detail


def test_request_confirmation_auto_checkin_without_slack():
    from app.api.checkins import request_checkin_confirmation
    from app.schemas.checkin import CheckInCreate

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None  # No existing checkin
    mock_user = MagicMock(employee_id=101)

    # Mock employee without slack
    mock_employee = MagicMock(id=101, name="John Doe", slack_user_id=None)
    def mock_query(model):
        m = MagicMock()
        if "Employee" in str(model):
            m.filter.return_value.first.return_value = mock_employee
        else:
            m.filter.return_value.first.return_value = None
        return m

    mock_db.query.side_effect = mock_query
    def fake_refresh(c):
        c.id = 1
    mock_db.refresh.side_effect = fake_refresh

    req = MagicMock()
    req.headers = {"x-forwarded-for": "38.20.140.122"}
    payload = CheckInCreate(work_mode="WFO", project_ids=[1], mood="great")

    res = request_checkin_confirmation(payload, req, mock_db, mock_user)
    assert res.status == "completed"
    assert res.checkin is not None


def test_request_slack_oauth_blocks_non_office_ip():
    from app.api.checkins import request_slack_oauth
    from app.schemas.checkin import CheckInCreate
    from fastapi import HTTPException

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    mock_user = MagicMock(employee_id=101)

    req = MagicMock()
    req.headers = {"x-forwarded-for": "198.51.100.99"}
    payload = CheckInCreate(work_mode="WFO", project_ids=[1], mood="great", office_floor="7", lunch_preference="none")

    with pytest.raises(HTTPException) as exc_info:
        request_slack_oauth(payload, req, mock_db, mock_user)

    assert exc_info.value.status_code == 400
    assert "office Wi-Fi" in exc_info.value.detail


def test_request_slack_oauth_success():
    from app.api.checkins import request_slack_oauth
    from app.schemas.checkin import CheckInCreate

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = MagicMock(id=101, slack_user_id="U101")
    # For daily checkin existing check
    def mock_query(model):
        m = MagicMock()
        if "DailyCheckIn" in str(model):
            m.filter.return_value.first.return_value = None
        else:
            m.filter.return_value.first.return_value = MagicMock(id=101, slack_user_id="U101")
        return m
    mock_db.query.side_effect = mock_query
    mock_user = MagicMock(employee_id=101)

    req = MagicMock()
    req.headers = {"x-forwarded-for": "38.20.140.122"}
    payload = CheckInCreate(work_mode="WFO", project_ids=[1], mood="great", office_floor="7", lunch_preference="none")

    res = request_slack_oauth(payload, req, mock_db, mock_user)
    assert "slack.com/openid/connect/authorize" in res.oauth_url
    assert "state=" in res.oauth_url


def test_slack_oauth_callback_blocks_non_office_ip():
    from app.api.checkins import slack_oauth_callback
    from app.services.auth_service import create_checkin_confirmation_token

    token = create_checkin_confirmation_token(
        employee_id=101,
        portal_ip="198.51.100.99",
        work_mode="WFO",
        project_ids=[1],
        mood="great",
        checkin_date="2026-09-10",
    )

    req = MagicMock()
    req.headers = {"x-forwarded-for": "198.51.100.99"}  # non-office IP
    mock_db = MagicMock()

    resp = slack_oauth_callback(req, code="test_code", state=token, db=mock_db)
    assert "checkin_error=office_ip_required" in resp.headers["location"]


def test_slack_oauth_callback_detects_proxy_mismatch(monkeypatch):
    from app.api.checkins import slack_oauth_callback
    from app.services.auth_service import create_checkin_confirmation_token

    token = create_checkin_confirmation_token(
        employee_id=101,
        portal_ip="38.20.140.122",
        work_mode="WFO",
        project_ids=[1],
        mood="great",
        checkin_date="2026-09-10",
    )

    # Mock exchange_slack_oauth_code returning Colleague's Slack account
    monkeypatch.setattr(
        "app.api.checkins.exchange_slack_oauth_code",
        lambda code, redirect_uri: {"ok": True, "sub": "U_COLLEAGUE_999", "email": "colleague@autonex.com"}
    )

    mock_db = MagicMock()
    mock_employee = MagicMock(id=101, slack_user_id="U_EMPLOYEE_101", email="emp101@autonex.com")
    mock_db.query.return_value.filter.return_value.first.return_value = mock_employee

    req = MagicMock()
    req.headers = {"x-forwarded-for": "38.20.140.122"}

    resp = slack_oauth_callback(req, code="test_code", state=token, db=mock_db)
    assert "checkin_error=account_mismatch" in resp.headers["location"]


def test_slack_oauth_callback_success(monkeypatch):
    from app.api.checkins import slack_oauth_callback
    from app.services.auth_service import create_checkin_confirmation_token

    token = create_checkin_confirmation_token(
        employee_id=101,
        portal_ip="38.20.140.122",
        work_mode="WFO",
        project_ids=[1],
        mood="great",
        checkin_date="2026-09-10",
        office_floor="9",
        lunch_preference="order_tiffin",
        tiffin_type="full_meal",
    )

    # Mock exchange_slack_oauth_code returning legitimate Employee's Slack account
    monkeypatch.setattr(
        "app.api.checkins.exchange_slack_oauth_code",
        lambda code, redirect_uri: {"ok": True, "sub": "U_EMPLOYEE_101", "email": "emp101@autonex.com"}
    )

    mock_db = MagicMock()
    mock_employee = MagicMock(id=101, slack_user_id="U_EMPLOYEE_101", email="emp101@autonex.com")
    def mock_query(model):
        m = MagicMock()
        if "DailyCheckIn" in str(model):
            m.filter.return_value.first.return_value = None
        else:
            m.filter.return_value.first.return_value = mock_employee
        return m
    mock_db.query.side_effect = mock_query

    req = MagicMock()
    req.headers = {"x-forwarded-for": "38.20.140.122"}

    resp = slack_oauth_callback(req, code="test_code", state=token, db=mock_db)
    assert "checkin_result=success" in resp.headers["location"]
    assert mock_db.add.called
    assert mock_db.commit.called


def test_slack_oauth_callback_extracts_sub_from_id_token(monkeypatch):
    from app.api.checkins import slack_oauth_callback
    from app.services.auth_service import create_checkin_confirmation_token
    from jose import jwt

    fake_id_token = jwt.encode({"sub": "U_EMPLOYEE_101", "aud": "client_123"}, "secret", algorithm="HS256")

    token = create_checkin_confirmation_token(
        employee_id=101,
        portal_ip="38.20.140.122",
        work_mode="WFO",
        project_ids=[1],
        mood="great",
        checkin_date="2026-09-10",
        office_floor="9",
        lunch_preference="order_tiffin",
        tiffin_type="full_meal",
    )

    # Mock exchange_slack_oauth_code returning only id_token and access_token (real Slack response)
    monkeypatch.setattr(
        "app.api.checkins.exchange_slack_oauth_code",
        lambda code, redirect_uri: {"ok": True, "id_token": fake_id_token, "access_token": "xoxp-123"}
    )

    mock_db = MagicMock()
    mock_employee = MagicMock(id=101, slack_user_id="U_EMPLOYEE_101")
    def mock_query(model):
        m = MagicMock()
        if "DailyCheckIn" in str(model):
            m.filter.return_value.first.return_value = None
        else:
            m.filter.return_value.first.return_value = mock_employee
        return m
    mock_db.query.side_effect = mock_query

    req = MagicMock()
    req.headers = {"x-forwarded-for": "38.20.140.122"}

    resp = slack_oauth_callback(req, code="test_code", state=token, db=mock_db)
    assert "checkin_result=success" in resp.headers["location"]
    assert mock_db.add.called


def test_slack_oauth_callback_detects_ip_mismatch():
    from app.api.checkins import slack_oauth_callback
    from app.services.auth_service import create_checkin_confirmation_token

    # Token initiated from office IP 1
    token = create_checkin_confirmation_token(
        employee_id=101,
        portal_ip="38.20.140.122",
        work_mode="WFO",
        project_ids=[1],
        mood="great",
        checkin_date="2026-09-10",
    )

    # Callback redirected from different IP (e.g. office IP 2 or other network)
    req = MagicMock()
    req.headers = {"x-forwarded-for": "103.54.189.22"}  # different IP
    mock_db = MagicMock()

    resp = slack_oauth_callback(req, code="test_code", state=token, db=mock_db)
    assert "checkin_error=ip_mismatch" in resp.headers["location"]



