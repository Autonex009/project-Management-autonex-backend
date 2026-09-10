"""Daily check-in/check-out API — attendance mode + today's project(s) + mood."""
import os
import ipaddress
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, List
from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException, Response, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.allocation import Allocation
from app.models.project import Project
from app.models.parent_project import MainProject
from app.models.wfh import WFHRequest
from app.models.daily_checkin import DailyCheckIn
from app.models.employee import Employee
from app.models.user import User
from app.services.auth_service import (
    get_current_user,
    require_role,
    create_checkin_confirmation_token,
    decode_checkin_confirmation_token,
    burn_checkin_token,
    is_checkin_token_burned,
)
from app.services.slack_service import (
    send_checkin_confirmation_message,
    try_get_or_cache_employee_slack_user_id,
    expire_slack_checkin_message,
    get_slack_oauth_redirect_uri,
    build_slack_oauth_authorize_url,
    exchange_slack_oauth_code,
)
from app.services.project_scope import can_act_on_project, has_full_access
from app.schemas.checkin import (
    CheckInCreate,
    CheckOutUpdate,
    CheckInResponse,
    TodayCheckInStatus,
    TeamCheckInRow,
    PaginatedTeamCheckIns,
    ConfirmResult,
    CheckInConfirmationResponse,
    SlackConfirmRequest,
    SlackOAuthRequestResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/checkins", tags=["checkins"])

IST = ZoneInfo("Asia/Kolkata")

DEFAULT_OFFICE_IPS = "38.20.140.122,103.54.189.22,27.0.150.66"


def _get_office_ips() -> set[str]:
    raw = os.getenv("OFFICE_IPS", DEFAULT_OFFICE_IPS)
    return {ip.strip() for ip in raw.split(",") if ip.strip()}


def _get_client_ip(request: Request) -> str:
    """Extract client IP handling reverse proxies, Cloudflare, and direct connections."""
    if not request:
        return ""
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _is_office_ip(client_ip: str) -> bool:
    """Verify if client IP matches authorized office public IPs or CIDR blocks."""
    if os.getenv("BYPASS_OFFICE_IP_CHECK", "false").lower() in ("true", "1"):
        return True
    if not client_ip:
        return False
    office_ips = _get_office_ips()
    if client_ip in office_ips:
        return True
    try:
        ip_obj = ipaddress.ip_address(client_ip)
        for allowed in office_ips:
            try:
                if "/" in allowed:
                    if ip_obj in ipaddress.ip_network(allowed, strict=False):
                        return True
                else:
                    if ip_obj == ipaddress.ip_address(allowed):
                        return True
            except ValueError:
                continue
    except ValueError:
        pass
    return False


def _get_ist_today():
    return datetime.now(IST).date()

def _get_ist_now():
    return datetime.now(IST)

def _require_employee(current_user: User) -> int:
    if not current_user.employee_id:
        raise HTTPException(status_code=400, detail="No employee record linked to this account.")
    return current_user.employee_id


def _get_scoped_project_ids(db: Session, user: User) -> set[int]:
    import time
    t0 = time.time()
    if has_full_access(user):
        return {r[0] for r in db.query(Project.id).all()}
        
    actor_id = user.employee_id
    if not actor_id:
        return set()
        
    all_projects = db.query(Project.id, Project.main_project_id, Project.assigned_employee_ids).all()
    main_pm_rows = db.query(MainProject.id, MainProject.program_manager_ids).all()
    actor_main_proj_ids = {m.id for m in main_pm_rows if str(actor_id) in [str(x) for x in (m.program_manager_ids or [])]}
    
    actor_allocations = db.query(Allocation.sub_project_id).filter(
        Allocation.employee_id == actor_id,
        Allocation.is_active == True
    ).all()
    actor_alloc_proj_ids = {r[0] for r in actor_allocations if r[0]}
    
    scoped = set()
    for p_id, p_main_id, p_assigned in all_projects:
        if str(actor_id) in [str(x) for x in (p_assigned or [])]:
            scoped.add(p_id)
            continue
        if p_main_id in actor_main_proj_ids:
            scoped.add(p_id)
            continue
        if p_id in actor_alloc_proj_ids:
            scoped.add(p_id)
            
    print(f"PROFILE _get_scoped_project_ids: {time.time()-t0:.3f}s")
    return scoped


def _build_paginated_checkins(db: Session, base_query, page: int, limit: int, kpis: dict, scoped_project_ids=None):
    import time
    t0 = time.time()
    total = base_query.count()
    t1 = time.time()
    results = base_query.order_by(Employee.name).offset((page - 1) * limit).limit(limit).all()
    t2 = time.time()
    
    if not results:
        return PaginatedTeamCheckIns(
            total=total, page=page, limit=limit, items=[],
            kpi_total=kpis.get("total", 0), kpi_checked_in=kpis.get("checked_in", 0), kpi_confirmed=kpis.get("confirmed", 0)
        )
        
    emp_ids = [emp.id for emp, _ in results]
    
    allocs = db.query(Allocation, Project.name).join(Project, Allocation.sub_project_id == Project.id).filter(
        Allocation.employee_id.in_(emp_ids), 
        Allocation.is_active == True
    ).all()
    t3 = time.time()
    
    proj_map = {}
    alloc_proj_ids = {}
    for a, p_name in allocs:
        proj_map.setdefault(a.employee_id, []).append(p_name)
        alloc_proj_ids.setdefault(a.employee_id, set()).add(a.sub_project_id)
        
    all_proj = {p.id: p.name for p in db.query(Project.id, Project.name).all()}
    t4 = time.time()
    
    items = []
    for emp, chk in results:
        alloc_pnames = proj_map.get(emp.id, [])
        emp_alloc_pids = alloc_proj_ids.get(emp.id, set())
        
        is_officially_allocated = True
        
        if chk and chk.project_ids:
            chk_pnames = [all_proj[pid] for pid in chk.project_ids if pid in all_proj]
            if "other" in chk.project_ids:
                chk_pnames.append("Other")
            if scoped_project_ids is not None:
                chk_pids = set(pid for pid in chk.project_ids if isinstance(pid, int))
                overlap = chk_pids.intersection(scoped_project_ids)
                if overlap and not overlap.intersection(emp_alloc_pids):
                    is_officially_allocated = False
        else:
            chk_pnames = []
            
        pnames = list(set(alloc_pnames + chk_pnames))
        
        items.append(TeamCheckInRow(
            employee_id=emp.id,
            name=emp.name,
            avatar_url=getattr(emp, "avatar_url", None),
            designation=emp.designation,
            project_names=pnames,
            checked_in=chk is not None,
            work_mode=chk.work_mode if chk else None,
            mood=chk.mood if chk else None,
            office_floor=chk.office_floor if chk else None,  # NEW
            lunch_preference=chk.lunch_preference if chk else None,  # NEW
            tiffin_type=chk.tiffin_type if chk else None,  # NEW
            checked_in_at=chk.checked_in_at if chk else None,
            checked_out_at=chk.checked_out_at if chk else None,
            pm_confirmed_at=chk.pm_confirmed_at if chk else None,
            is_officially_allocated=is_officially_allocated,
        ))
    t5 = time.time()
    print(f"PROFILE build: count={t1-t0:.3f}s results={t2-t1:.3f}s allocs={t3-t2:.3f}s all_proj={t4-t3:.3f}s loop={t5-t4:.3f}s")
        
    return PaginatedTeamCheckIns(
        total=total, page=page, limit=limit, items=items,
        kpi_total=kpis.get("total", 0), kpi_checked_in=kpis.get("checked_in", 0), kpi_confirmed=kpis.get("confirmed", 0)
    )


@router.get("/today", response_model=TodayCheckInStatus)
def get_today_status(
    response: Response,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    employee_id = _require_employee(current_user)
    today = _get_ist_today()

    existing = (
        db.query(DailyCheckIn)
        .filter(DailyCheckIn.employee_id == employee_id, DailyCheckIn.checkin_date == today)
        .first()
    )

    allocs = (
        db.query(Allocation)
        .filter(Allocation.employee_id == employee_id, Allocation.is_active == True)
        .all()
    )
    project_ids = list({a.sub_project_id for a in allocs if a.sub_project_id})
    projects = db.query(Project).filter(Project.id.in_(project_ids)).all() if project_ids else []
    project_options = [{"project_id": p.id, "project_name": p.name} for p in projects]

    approved_wfh_today = (
        db.query(WFHRequest)
        .filter(
            WFHRequest.employee_id == employee_id,
            WFHRequest.status == "approved",
            WFHRequest.wfh_date <= today,
        )
        .filter((WFHRequest.end_date == None) | (WFHRequest.end_date >= today))
        .first()
    )

    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    slack_id = employee.slack_user_id if employee else None
    if not slack_id and employee:
        slack_id = try_get_or_cache_employee_slack_user_id(db, employee)

    client_ip = _get_client_ip(http_request)
    is_office = _is_office_ip(client_ip)

    return TodayCheckInStatus(
        already_checked_in=existing is not None,
        checkin=existing,
        project_options=project_options,
        suggested_work_mode="WFH" if approved_wfh_today else "WFO",
        is_office_network=is_office,
        has_slack=bool(slack_id),
    )


@router.post("", response_model=CheckInResponse)
def submit_checkin(
    payload: CheckInCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    employee_id = _require_employee(current_user)
    today = _get_ist_today()

    existing = (
        db.query(DailyCheckIn)
        .filter(DailyCheckIn.employee_id == employee_id, DailyCheckIn.checkin_date == today)
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="You've already checked in today.")

    if payload.work_mode == "WFO":
        client_ip = _get_client_ip(http_request)
        if not _is_office_ip(client_ip):
            logger.warning(
                "[checkin] Blocked WFO check-in for employee_id=%s from non-office IP '%s'",
                employee_id,
                client_ip,
            )
            raise HTTPException(
                status_code=400,
                detail="Please connect to the office Wi-Fi or disconnect VPN service.",
            )

    checkin = DailyCheckIn(
        employee_id=employee_id,
        checkin_date=today,
        work_mode=payload.work_mode,
        project_ids=payload.project_ids,
        mood=payload.mood,
        office_floor=payload.office_floor,
        lunch_preference=payload.lunch_preference,
        tiffin_type=payload.tiffin_type,
        checked_in_at=_get_ist_now(),
    )
    db.add(checkin)
    db.commit()
    db.refresh(checkin)
    return checkin


@router.post("/request-confirmation", response_model=CheckInConfirmationResponse)
def request_checkin_confirmation(
    payload: CheckInCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Initiate check-in. If employee has Slack, send confirmation link to Slack DM. Otherwise check in automatically."""
    employee_id = _require_employee(current_user)
    today = _get_ist_today()

    existing = (
        db.query(DailyCheckIn)
        .filter(DailyCheckIn.employee_id == employee_id, DailyCheckIn.checkin_date == today)
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="You've already checked in today.")

    portal_ip = _get_client_ip(http_request)

    if payload.work_mode == "WFO":
        if not _is_office_ip(portal_ip):
            logger.warning(
                "[checkin] Blocked WFO request-confirmation for employee_id=%s from non-office IP '%s'",
                employee_id,
                portal_ip,
            )
            raise HTTPException(
                status_code=400,
                detail="Please connect to the office Wi-Fi or disconnect VPN service.",
            )

    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee record not found.")

    slack_id = employee.slack_user_id
    if not slack_id:
        slack_id = try_get_or_cache_employee_slack_user_id(db, employee)

    # If employee does not have a Slack ID, check them in automatically!
    if not slack_id:
        logger.info("[checkin] Employee %s has no Slack ID; checking in automatically.", employee_id)
        checkin = DailyCheckIn(
            employee_id=employee_id,
            checkin_date=today,
            work_mode=payload.work_mode,
            project_ids=payload.project_ids,
            mood=payload.mood,
            office_floor=payload.office_floor,
            lunch_preference=payload.lunch_preference,
            tiffin_type=payload.tiffin_type,
            checked_in_at=_get_ist_now(),
        )
        db.add(checkin)
        db.commit()
        db.refresh(checkin)
        return CheckInConfirmationResponse(
            status="completed",
            message="Checked in successfully!",
            checkin=checkin,
        )

    # Generate signed JWT confirmation token (90 seconds expiry)
    token = create_checkin_confirmation_token(
        employee_id=employee_id,
        portal_ip=portal_ip,
        work_mode=payload.work_mode,
        project_ids=payload.project_ids,
        mood=payload.mood,
        checkin_date=str(today),
        expires_seconds=90,
        office_floor=payload.office_floor,
        lunch_preference=payload.lunch_preference,
        tiffin_type=payload.tiffin_type,
    )

    origin = http_request.headers.get("origin") or http_request.headers.get("referer") or ""
    if origin:
        from urllib.parse import urlparse
        parsed = urlparse(origin)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
    else:
        base_url = (os.getenv("FRONTEND_URL") or "http://localhost:5173").strip().rstrip("/")

    verification_url = f"{base_url}/verify-checkin?token={token}"

    proj_names = []
    if payload.project_ids:
        projs = db.query(Project).filter(Project.id.in_(payload.project_ids)).all()
        proj_names = [p.name for p in projs]

    sent = send_checkin_confirmation_message(
        slack_user_id=slack_id,
        employee_name=employee.name,
        work_mode=payload.work_mode,
        project_names=proj_names,
        portal_ip=portal_ip,
        verification_url=verification_url,
        expires_seconds=90,
        employee_id=employee_id,
    )

    if not sent:
        logger.warning(
            "[checkin] Slack message failed for employee_id=%s. Checking in automatically as fallback.",
            employee_id,
        )
        checkin = DailyCheckIn(
            employee_id=employee_id,
            checkin_date=today,
            work_mode=payload.work_mode,
            project_ids=payload.project_ids,
            mood=payload.mood,
            office_floor=payload.office_floor,
            lunch_preference=payload.lunch_preference,
            tiffin_type=payload.tiffin_type,
            checked_in_at=_get_ist_now(),
        )
        db.add(checkin)
        db.commit()
        db.refresh(checkin)
        return CheckInConfirmationResponse(
            status="completed",
            message="Slack message could not be sent. Checked in automatically!",
            checkin=checkin,
        )

    return CheckInConfirmationResponse(
        status="pending_slack",
        message="Confirmation link sent to your Slack DM. Please click it within 90 seconds to complete check-in.",
        expires_in=90,
    )


@router.post("/confirm-slack", response_model=CheckInResponse)
def confirm_slack_checkin(
    req: SlackConfirmRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Validates Slack confirmation token, single-use burn, user identity, IP matching, and finalizes check-in."""
    try:
        payload = decode_checkin_confirmation_token(req.token)
    except Exception as exc:
        logger.warning("[checkin/confirm-slack] Invalid or expired token: %s", exc)
        raise HTTPException(
            status_code=400,
            detail="Confirmation link is invalid or expired. Please initiate a new check-in from the portal.",
        )

    # Verify that the logged-in user matches the employee for whom the token was issued
    user_employee_id = _require_employee(current_user)
    employee_id = payload.get("employee_id")
    if user_employee_id != employee_id:
        logger.warning(
            "[checkin/confirm-slack] Account mismatch: current_user.employee_id=%s != token.employee_id=%s",
            user_employee_id,
            employee_id,
        )
        raise HTTPException(
            status_code=403,
            detail="Account mismatch: This verification link was generated for another employee. You cannot verify someone else's check-in.",
        )

    from datetime import date
    checkin_date_str = payload.get("checkin_date")
    today = date.fromisoformat(checkin_date_str) if checkin_date_str else _get_ist_today()

    # If the user is already checked in for today, return existing record idempotently
    existing = (
        db.query(DailyCheckIn)
        .filter(DailyCheckIn.employee_id == employee_id, DailyCheckIn.checkin_date == today)
        .first()
    )
    if existing:
        return existing

    jti = payload.get("jti")
    if not jti or is_checkin_token_burned(jti):
        logger.warning("[checkin/confirm-slack] Token already burned / used: jti=%s", jti)
        raise HTTPException(
            status_code=400,
            detail="This confirmation link has already been used or invalidated. Please request a new confirmation link from the portal.",
        )

    # Immediately burn the token so that any concurrent or subsequent requests are blocked
    burn_checkin_token(jti)

    portal_ip = payload.get("portal_ip")
    click_ip = _get_client_ip(http_request)
    work_mode = payload.get("work_mode")

    logger.info(
        "[checkin/confirm-slack] Verifying check-in for employee_id=%s. portal_ip=%s, click_ip=%s",
        employee_id,
        portal_ip,
        click_ip,
    )

    # ONLY check in when both IPs match
    if portal_ip and click_ip != portal_ip:
        logger.warning(
            "[checkin/confirm-slack] IP MISMATCH for employee_id=%s: portal_ip '%s' != click_ip '%s'",
            employee_id,
            portal_ip,
            click_ip,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"IP mismatch: Check-in was requested from IP {portal_ip}, but confirmation was clicked from IP {click_ip}. "
                "Both actions must be performed from the same Wi-Fi / IP network."
            ),
        )

    # For WFO, click_ip must also be an authorized office IP
    if work_mode == "WFO" and not _is_office_ip(click_ip):
        logger.warning(
            "[checkin/confirm-slack] Blocked WFO confirm for employee_id=%s: click_ip '%s' not in office IPs",
            employee_id,
            click_ip,
        )
        raise HTTPException(
            status_code=400,
            detail="Please connect to the office Wi-Fi or disconnect VPN service.",
        )

    checkin = DailyCheckIn(
        employee_id=employee_id,
        checkin_date=today,
        work_mode=work_mode,
        project_ids=payload.get("project_ids", []),
        mood=payload.get("mood"),
        office_floor=payload.get("office_floor"),
        lunch_preference=payload.get("lunch_preference"),
        tiffin_type=payload.get("tiffin_type"),
        checked_in_at=_get_ist_now(),
    )
    db.add(checkin)
    db.commit()
    db.refresh(checkin)

    # Deactivate the Slack message so the button is removed and cannot be reused
    expire_slack_checkin_message(employee_id, getattr(current_user, "name", ""))

    logger.info("[checkin/confirm-slack] Check-in successfully recorded for employee_id=%s", employee_id)
    return checkin


@router.post("/request-slack-oauth", response_model=SlackOAuthRequestResponse)
def request_slack_oauth(
    payload: CheckInCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Initiates check-in via Slack OpenID Connect (OAuth 2.0)."""
    employee_id = _require_employee(current_user)
    today = _get_ist_today()

    existing = (
        db.query(DailyCheckIn)
        .filter(DailyCheckIn.employee_id == employee_id, DailyCheckIn.checkin_date == today)
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="You've already checked in today.")

    portal_ip = _get_client_ip(http_request)

    if payload.work_mode == "WFO":
        if not _is_office_ip(portal_ip):
            logger.warning(
                "[checkin] Blocked WFO request-slack-oauth for employee_id=%s from non-office IP '%s'",
                employee_id,
                portal_ip,
            )
            raise HTTPException(
                status_code=400,
                detail="Please connect to the office Wi-Fi or disconnect VPN service.",
            )

    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee record not found.")

    # Generate signed JWT confirmation token (30 seconds expiry)
    token = create_checkin_confirmation_token(
        employee_id=employee_id,
        portal_ip=portal_ip,
        work_mode=payload.work_mode,
        project_ids=payload.project_ids,
        mood=payload.mood,
        checkin_date=str(today),
        expires_seconds=30,
        office_floor=payload.office_floor,
        lunch_preference=payload.lunch_preference,
        tiffin_type=payload.tiffin_type,
    )

    redirect_uri = get_slack_oauth_redirect_uri(http_request)
    oauth_url = build_slack_oauth_authorize_url(state=token, redirect_uri=redirect_uri)

    logger.info("[checkin] Generated Slack OAuth authorize URL for employee_id=%s, redirect_uri=%s (30s expiry)", employee_id, redirect_uri)
    return SlackOAuthRequestResponse(oauth_url=oauth_url, expires_in=30)


@router.get("/slack-oauth-callback")
def slack_oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Callback endpoint redirected from Slack OpenID Connect authorization."""
    frontend_url = (os.getenv("FRONTEND_URL") or os.getenv("APP_URL") or "http://localhost:5173").strip()
    frontend_url = frontend_url.replace("\\n", "").replace("\n", "").replace('"', '').rstrip("/")

    if error:
        logger.warning("[slack-oauth-callback] Slack returned error: %s - %s", error, error_description)
        return RedirectResponse(f"{frontend_url}/dashboard?checkin_error=slack_access_denied&oauth_popup=1")

    if not code or not state:
        logger.warning("[slack-oauth-callback] Missing code or state")
        return RedirectResponse(f"{frontend_url}/dashboard?checkin_error=invalid_request&oauth_popup=1")

    try:
        payload = decode_checkin_confirmation_token(state)
    except Exception as exc:
        logger.warning("[slack-oauth-callback] Invalid or expired token: %s", exc)
        return RedirectResponse(f"{frontend_url}/dashboard?checkin_error=token_expired&oauth_popup=1")

    jti = payload.get("jti")
    if not jti or is_checkin_token_burned(jti):
        logger.warning("[slack-oauth-callback] Token already burned: jti=%s", jti)
        return RedirectResponse(f"{frontend_url}/dashboard?checkin_error=token_already_used&oauth_popup=1")

    employee_id = payload.get("employee_id")
    work_mode = payload.get("work_mode")
    checkin_date_str = payload.get("checkin_date")
    from datetime import date
    today = date.fromisoformat(checkin_date_str) if checkin_date_str else _get_ist_today()

    portal_ip = payload.get("portal_ip")
    client_ip = _get_client_ip(request)
    logger.info("[slack-oauth-callback] Callback received: portal_ip=%s, client_ip=%s, employee_id=%s, work_mode=%s", portal_ip, client_ip, employee_id, work_mode)

    # STRICT CHECK: Confirmation must originate from the exact same network as initiation
    if portal_ip and client_ip != portal_ip:
        logger.warning(
            "[slack-oauth-callback] IP MISMATCH for employee_id=%s: portal_ip '%s' != client_ip '%s'",
            employee_id,
            portal_ip,
            client_ip,
        )
        return RedirectResponse(f"{frontend_url}/dashboard?checkin_error=ip_mismatch&portal_ip={portal_ip}&client_ip={client_ip}&oauth_popup=1")

    if work_mode == "WFO" and not _is_office_ip(client_ip):
        logger.warning("[slack-oauth-callback] Blocked WFO checkin from non-office IP '%s' for employee_id=%s", client_ip, employee_id)
        return RedirectResponse(f"{frontend_url}/dashboard?checkin_error=office_ip_required&ip={client_ip}&oauth_popup=1")

    redirect_uri = get_slack_oauth_redirect_uri(request)
    try:
        token_resp = exchange_slack_oauth_code(code=code, redirect_uri=redirect_uri)
    except Exception as exc:
        logger.error("[slack-oauth-callback] Token exchange error: %s", exc)
        return RedirectResponse(f"{frontend_url}/dashboard?checkin_error=slack_exchange_failed&oauth_popup=1")

    if not token_resp.get("ok"):
        logger.error("[slack-oauth-callback] Slack returned not ok: %s", token_resp)
        return RedirectResponse(f"{frontend_url}/dashboard?checkin_error=slack_exchange_failed&oauth_popup=1")

    slack_sub = token_resp.get("sub")
    if not slack_sub and "id_token" in token_resp:
        try:
            from jose import jwt
            claims = jwt.get_unverified_claims(token_resp["id_token"])
            slack_sub = claims.get("sub") or claims.get("https://slack.com/user_id")
        except Exception as exc:
            logger.warning("[slack-oauth-callback] Failed to decode id_token: %s", exc)

    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        return RedirectResponse(f"{frontend_url}/dashboard?checkin_error=employee_not_found&oauth_popup=1")

    # If employee record does not have a cached slack_user_id, try to fetch it
    if not employee.slack_user_id:
        employee.slack_user_id = try_get_or_cache_employee_slack_user_id(db, employee)

    # STRICT CHECK: Match ONLY on Slack user ID (no email check)
    if not slack_sub or not employee.slack_user_id or employee.slack_user_id != slack_sub:
        logger.warning(
            "[slack-oauth-callback] PROXY ATTEMPT: Authenticated Slack user ID '%s' does not match employee %s (expected slack_user_id='%s')",
            slack_sub,
            employee.id,
            employee.slack_user_id,
        )
        return RedirectResponse(f"{frontend_url}/dashboard?checkin_error=account_mismatch&oauth_popup=1")

    burn_checkin_token(jti)

    existing = (
        db.query(DailyCheckIn)
        .filter(DailyCheckIn.employee_id == employee_id, DailyCheckIn.checkin_date == today)
        .first()
    )
    if not existing:
        checkin = DailyCheckIn(
            employee_id=employee_id,
            checkin_date=today,
            work_mode=work_mode,
            project_ids=payload.get("project_ids", []),
            mood=payload.get("mood"),
            office_floor=payload.get("office_floor"),
            lunch_preference=payload.get("lunch_preference"),
            tiffin_type=payload.get("tiffin_type"),
            checked_in_at=_get_ist_now(),
        )
        db.add(checkin)
        db.commit()
        db.refresh(checkin)

    logger.info("[slack-oauth-callback] Successfully checked in employee_id=%s via Slack OAuth!", employee_id)
    return RedirectResponse(f"{frontend_url}/dashboard?checkin_result=success&oauth_popup=1")


@router.post("/checkout", response_model=CheckInResponse)
def submit_checkout(
    payload: CheckOutUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    employee_id = _require_employee(current_user)
    today = _get_ist_today()

    checkin = (
        db.query(DailyCheckIn)
        .filter(DailyCheckIn.employee_id == employee_id, DailyCheckIn.checkin_date == today)
        .first()
    )
    if not checkin:
        raise HTTPException(status_code=400, detail="You haven't checked in today yet.")
    if checkin.checked_out_at:
        raise HTTPException(status_code=400, detail="You've already checked out today.")

    checkin.checked_out_at = _get_ist_now()
    if payload.mood is not None:
        checkin.mood = payload.mood
    db.commit()
    db.refresh(checkin)
    return checkin

@router.get("/team-today", response_model=PaginatedTeamCheckIns)
def get_team_today(
    page: int = 1,
    limit: int = 50,
    search: str = "",
    status: str = "",
    work_mode: str = "",
    project_id: int = None,
    time_filter: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("pm", "team_lead")),
):
    import time
    t0 = time.time()
    today = _get_ist_today()
    
    scoped_project_ids = _get_scoped_project_ids(db, current_user)
    t1 = time.time()
    
    if not scoped_project_ids:
        return PaginatedTeamCheckIns(total=0, page=page, limit=limit, items=[])

    allocs = db.query(Allocation.employee_id).filter(
        Allocation.sub_project_id.in_(scoped_project_ids), 
        Allocation.is_active == True
    ).all()
    allocated_emp_ids = {a.employee_id for a in allocs if a.employee_id}

    all_today_checkins = db.query(DailyCheckIn.employee_id, DailyCheckIn.project_ids, DailyCheckIn.pm_confirmed_at).filter(DailyCheckIn.checkin_date == today).all()
    checked_in_emp_ids = set()
    for c in all_today_checkins:
        if set(c.project_ids or []).intersection(scoped_project_ids):
            checked_in_emp_ids.add(c.employee_id)
            
    visible_emp_ids = allocated_emp_ids.union(checked_in_emp_ids)
    t2 = time.time()
    
    if not visible_emp_ids:
        return PaginatedTeamCheckIns(total=0, page=page, limit=limit, items=[])

    kpis = {
        "total": len(visible_emp_ids),
        "checked_in": len(checked_in_emp_ids.intersection(visible_emp_ids)),
        "confirmed": sum(1 for c in all_today_checkins if c.employee_id in visible_emp_ids and c.pm_confirmed_at is not None)
    }
    t3 = time.time()
        
    query = db.query(Employee, DailyCheckIn).outerjoin(
        DailyCheckIn, 
        (Employee.id == DailyCheckIn.employee_id) & (DailyCheckIn.checkin_date == today)
    ).filter(Employee.id.in_(visible_emp_ids))
    
    if search:
        query = query.filter(Employee.name.ilike(f"%{search}%"))
    if status == "checked_in":
        query = query.filter(DailyCheckIn.id.isnot(None))
    elif status == "pending":
        query = query.filter(DailyCheckIn.id.is_(None))
    if work_mode:
        query = query.filter(DailyCheckIn.work_mode == work_mode)
        
    if project_id:
        allocs_proj = db.query(Allocation.employee_id).filter(
            Allocation.sub_project_id == project_id, 
            Allocation.is_active == True
        ).all()
        proj_emp_ids = {a.employee_id for a in allocs_proj if a.employee_id}
        chk_proj = db.query(DailyCheckIn.employee_id).filter(
            DailyCheckIn.checkin_date == today,
            DailyCheckIn.project_ids.contains([project_id])
        ).all()
        proj_emp_ids.update({c.employee_id for c in chk_proj})
        query = query.filter(Employee.id.in_(proj_emp_ids))
        
    if time_filter == "late":
        from datetime import time as dtime
        from datetime import timezone
        late_threshold_ist = datetime.combine(today, dtime(10, 0), tzinfo=IST)
        late_threshold_utc = late_threshold_ist.astimezone(timezone.utc)
        query = query.filter(DailyCheckIn.checked_in_at > late_threshold_utc)
        
    res = _build_paginated_checkins(db, query, page, limit, kpis, scoped_project_ids)
    t4 = time.time()
    
    print(f"PROFILE team_today: scope={t1-t0:.3f}s setup={t2-t1:.3f}s kpis={t3-t2:.3f}s build={t4-t3:.3f}s TOTAL={t4-t0:.3f}s")
    return res


class ConfirmRequest(BaseModel):
    employee_ids: Optional[List[int]] = None

@router.post("/team/confirm", response_model=ConfirmResult)
def confirm_team_today(
    req: ConfirmRequest = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("pm", "team_lead")),
):
    today = _get_ist_today()
    scoped_project_ids = _get_scoped_project_ids(db, current_user)
    
    if not scoped_project_ids:
        return ConfirmResult(confirmed=0)

    allocs = db.query(Allocation).filter(Allocation.sub_project_id.in_(scoped_project_ids), Allocation.is_active == True).all()
    roster_emp_ids = {a.employee_id for a in allocs if a.employee_id}
    
    query = db.query(DailyCheckIn).filter(
        DailyCheckIn.checkin_date == today,
        DailyCheckIn.pm_confirmed_at.is_(None)
    )
    
    if req and req.employee_ids is not None:
        query = query.filter(DailyCheckIn.employee_id.in_(req.employee_ids))
        
    all_today_checkins = query.all()
    
    to_confirm = []
    now = _get_ist_now()
    
    for c in all_today_checkins:
        c_pids = set(c.project_ids or [])
        if c.employee_id in roster_emp_ids or c_pids.intersection(scoped_project_ids):
            c.pm_confirmed_at = now
            c.pm_confirmed_by = current_user.id
            to_confirm.append(c)

    if to_confirm:
        db.commit()
        
    return ConfirmResult(confirmed=len(to_confirm))


@router.get("/admin/paginated", response_model=PaginatedTeamCheckIns)
def get_admin_checkins_paginated(
    page: int = 1,
    limit: int = 50,
    search: str = "",
    status: str = "",
    work_mode: str = "",
    project_id: int = None,
    time_filter: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "hr")),
):
    today = _get_ist_today()
    
    total_active = db.query(Employee).filter(Employee.status == "active").count()
    checked_in_active = db.query(DailyCheckIn.employee_id).join(Employee, DailyCheckIn.employee_id == Employee.id).filter(Employee.status == "active", DailyCheckIn.checkin_date == today).count()
    confirmed_active = db.query(DailyCheckIn.employee_id).join(Employee, DailyCheckIn.employee_id == Employee.id).filter(Employee.status == "active", DailyCheckIn.checkin_date == today, DailyCheckIn.pm_confirmed_at.isnot(None)).count()
    
    kpis = {
        "total": total_active,
        "checked_in": checked_in_active,
        "confirmed": confirmed_active
    }
    
    query = db.query(Employee, DailyCheckIn).outerjoin(
        DailyCheckIn, 
        (Employee.id == DailyCheckIn.employee_id) & (DailyCheckIn.checkin_date == today)
    ).filter(Employee.status == "active")
    
    if search:
        query = query.filter(Employee.name.ilike(f"%{search}%"))
    if status == "checked_in":
        query = query.filter(DailyCheckIn.id.isnot(None))
    elif status == "pending":
        query = query.filter(DailyCheckIn.id.is_(None))
    if work_mode:
        query = query.filter(DailyCheckIn.work_mode == work_mode)
        
    if project_id:
        allocs_proj = db.query(Allocation.employee_id).filter(
            Allocation.sub_project_id == project_id, 
            Allocation.is_active == True
        ).all()
        proj_emp_ids = {a.employee_id for a in allocs_proj if a.employee_id}
        chk_proj = db.query(DailyCheckIn.employee_id).filter(
            DailyCheckIn.checkin_date == today,
            DailyCheckIn.project_ids.contains([project_id])
        ).all()
        proj_emp_ids.update({c.employee_id for c in chk_proj})
        query = query.filter(Employee.id.in_(proj_emp_ids))
        
    if time_filter == "late":
        from datetime import time as dtime
        from datetime import timezone
        late_threshold_ist = datetime.combine(today, dtime(10, 0), tzinfo=IST)
        late_threshold_utc = late_threshold_ist.astimezone(timezone.utc)
        query = query.filter(DailyCheckIn.checked_in_at > late_threshold_utc)
        
    return _build_paginated_checkins(db, query, page, limit, kpis, scoped_project_ids=None)

from app.schemas.checkin import MatrixResponse, MatrixRow
import calendar

def _get_matrix_data(db: Session, month_year: str, employee_ids: set) -> MatrixResponse:
    y_str, m_str = month_year.split("-")
    y, m = int(y_str), int(m_str)
    _, days_in_month = calendar.monthrange(y, m)
    
    current_month_year = _get_ist_today().strftime("%Y-%m")
    
    emp_map = {}
    if employee_ids:
        emps = db.query(Employee).filter(Employee.id.in_(employee_ids), Employee.status == "active").all()
    else:
        emps = db.query(Employee).filter(Employee.status == "active").all()
        employee_ids = {e.id for e in emps}
        
    for e in emps:
        emp_map[e.id] = MatrixRow(
            employee_id=e.id, 
            name=e.name, 
            avatar_url=getattr(e, "avatar_url", None), 
            designation=e.designation,
            checkins={}
        )
        
    if month_year == current_month_year:
        from sqlalchemy import text
        res = db.execute(text("""
            SELECT employee_id, EXTRACT(DAY FROM checkin_date)::TEXT as day_str, 
                   TO_CHAR(checked_in_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI') as time, work_mode
            FROM daily_checkins
            WHERE TO_CHAR(checkin_date, 'YYYY-MM') = :my
        """), {"my": month_year}).fetchall()
        for r in res:
            eid, dstr, t, mode = r
            if eid in emp_map:
                emp_map[eid].checkins[dstr] = {"time": t, "mode": mode}
    else:
        from sqlalchemy import text
        res = db.execute(text("""
            SELECT employee_id, checkin_matrix
            FROM historical_checkins_matrix
            WHERE month_year = :my
        """), {"my": month_year}).fetchall()
        for r in res:
            eid, matrix = r
            if eid in emp_map and matrix:
                emp_map[eid].checkins = matrix
                
    rows = list(emp_map.values())
    rows.sort(key=lambda x: x.name)
    return MatrixResponse(month_year=month_year, days_in_month=days_in_month, rows=rows)

@router.get("/team/matrix", response_model=MatrixResponse)
def get_team_matrix(
    month_year: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("pm", "team_lead")),
):
    scoped_project_ids = _get_scoped_project_ids(db, current_user)
    if not scoped_project_ids:
        y, m = map(int, month_year.split("-"))
        _, d = calendar.monthrange(y, m)
        return MatrixResponse(month_year=month_year, days_in_month=d, rows=[])
        
    allocs = db.query(Allocation.employee_id).filter(
        Allocation.sub_project_id.in_(scoped_project_ids), 
        Allocation.is_active == True
    ).all()
    emp_ids = {a.employee_id for a in allocs if a.employee_id}
    return _get_matrix_data(db, month_year, emp_ids)

@router.get("/admin/matrix", response_model=MatrixResponse)
def get_admin_matrix(
    month_year: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "hr")),
):
    return _get_matrix_data(db, month_year, set())
