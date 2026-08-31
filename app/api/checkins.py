"""Daily check-in/check-out API — attendance mode + today's project(s) + mood."""
import logging
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.allocation import Allocation
from app.models.project import Project
from app.models.wfh import WFHRequest
from app.models.daily_checkin import DailyCheckIn
from app.models.employee import Employee
from app.models.user import User
from app.services.auth_service import get_current_user, require_role
from app.services.project_scope import can_act_on_project, has_full_access
from app.schemas.checkin import (
    CheckInCreate,
    CheckOutUpdate,
    CheckInResponse,
    TodayCheckInStatus,
    TeamCheckInRow,
    TeamCheckInSummary,
    ConfirmResult,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/checkins", tags=["checkins"], dependencies=[Depends(get_current_user)])


def _require_employee(current_user: User) -> int:
    if not current_user.employee_id:
        raise HTTPException(status_code=400, detail="No employee record linked to this account.")
    return current_user.employee_id


@router.get("/today", response_model=TodayCheckInStatus)
def get_today_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    employee_id = _require_employee(current_user)
    today = date.today()

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
        .filter((WFHRequest.end_date == None) | (WFHRequest.end_date >= today))  # noqa: E711
        .first()
    )

    return TodayCheckInStatus(
        already_checked_in=existing is not None,
        checkin=existing,
        project_options=project_options,
        suggested_work_mode="WFH" if approved_wfh_today else "WFO",
    )


@router.post("", response_model=CheckInResponse)
def submit_checkin(
    payload: CheckInCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    employee_id = _require_employee(current_user)
    today = date.today()

    existing = (
        db.query(DailyCheckIn)
        .filter(DailyCheckIn.employee_id == employee_id, DailyCheckIn.checkin_date == today)
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="You've already checked in today.")

    checkin = DailyCheckIn(
        employee_id=employee_id,
        checkin_date=today,
        work_mode=payload.work_mode,
        project_ids=payload.project_ids,
        mood=payload.mood,
        checked_in_at=datetime.utcnow(),
    )
    db.add(checkin)
    db.commit()
    db.refresh(checkin)
    return checkin


@router.post("/checkout", response_model=CheckInResponse)
def submit_checkout(
    payload: CheckOutUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    employee_id = _require_employee(current_user)
    today = date.today()

    checkin = (
        db.query(DailyCheckIn)
        .filter(DailyCheckIn.employee_id == employee_id, DailyCheckIn.checkin_date == today)
        .first()
    )
    if not checkin:
        raise HTTPException(status_code=400, detail="You haven't checked in today yet.")
    if checkin.checked_out_at:
        raise HTTPException(status_code=400, detail="You've already checked out today.")

    checkin.checked_out_at = datetime.utcnow()
    if payload.mood is not None:
        checkin.mood = payload.mood
    db.commit()
    db.refresh(checkin)
    return checkin


def _scoped_roster(db: Session, current_user: User) -> dict[int, list[int]]:
    """employee_id -> [project_id, ...] for everyone on a project this caller runs.

    Mirrors GET /employees/team-data's scoping (app/api/employees.py) — same "which
    projects can this caller act on" question via project_scope, applied here to
    check-ins instead of leaves/allocations.
    """
    all_projects = db.query(Project).all()
    if has_full_access(current_user):
        scoped_projects = all_projects
    else:
        scoped_projects = [p for p in all_projects if can_act_on_project(db, current_user, p)]
    scoped_project_ids = [p.id for p in scoped_projects]
    if not scoped_project_ids:
        return {}

    allocs = (
        db.query(Allocation)
        .filter(Allocation.sub_project_id.in_(scoped_project_ids), Allocation.is_active == True)
        .all()
    )
    roster: dict[int, list[int]] = {}
    for a in allocs:
        if not a.employee_id or not a.sub_project_id:
            continue
        roster.setdefault(a.employee_id, []).append(a.sub_project_id)
    return roster


@router.get("/team-today", response_model=TeamCheckInSummary)
def get_team_today(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("pm", "team_lead")),
):
    today = date.today()
    roster = _scoped_roster(db, current_user)
    if not roster:
        return TeamCheckInSummary(date=today, total=0, checked_in=0, confirmed=0, rows=[])

    employee_ids = list(roster.keys())
    employees = {e.id: e for e in db.query(Employee).filter(Employee.id.in_(employee_ids)).all()}

    all_project_ids = {pid for pids in roster.values() for pid in pids}
    projects = {p.id: p.name for p in db.query(Project).filter(Project.id.in_(all_project_ids)).all()}

    checkins = {
        c.employee_id: c
        for c in db.query(DailyCheckIn).filter(
            DailyCheckIn.employee_id.in_(employee_ids), DailyCheckIn.checkin_date == today
        ).all()
    }

    rows = []
    for employee_id, project_ids in roster.items():
        emp = employees.get(employee_id)
        if not emp:
            continue
        checkin = checkins.get(employee_id)
        rows.append(TeamCheckInRow(
            employee_id=employee_id,
            name=emp.name,
            avatar_url=getattr(emp, "avatar_url", None),
            designation=emp.designation,
            project_names=[projects[pid] for pid in project_ids if pid in projects],
            checked_in=checkin is not None,
            work_mode=checkin.work_mode if checkin else None,
            mood=checkin.mood if checkin else None,
            checked_in_at=checkin.checked_in_at if checkin else None,
            checked_out_at=checkin.checked_out_at if checkin else None,
            pm_confirmed_at=checkin.pm_confirmed_at if checkin else None,
        ))
    rows.sort(key=lambda r: r.name or "")

    return TeamCheckInSummary(
        date=today,
        total=len(rows),
        checked_in=sum(1 for r in rows if r.checked_in),
        confirmed=sum(1 for r in rows if r.pm_confirmed_at),
        rows=rows,
    )


@router.post("/team/confirm", response_model=ConfirmResult)
def confirm_team_today(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("pm", "team_lead")),
):
    """Confirm today's roster in one action — stamps every checked-in, not-yet-confirmed
    employee in this PM/lead's scope. Employees who haven't checked in yet have no row to
    confirm; they simply show as not-checked-in until they do."""
    today = date.today()
    roster = _scoped_roster(db, current_user)
    if not roster:
        return ConfirmResult(confirmed=0)

    employee_ids = list(roster.keys())
    to_confirm = (
        db.query(DailyCheckIn)
        .filter(
            DailyCheckIn.employee_id.in_(employee_ids),
            DailyCheckIn.checkin_date == today,
            DailyCheckIn.pm_confirmed_at.is_(None),
        )
        .all()
    )
    now = datetime.utcnow()
    for checkin in to_confirm:
        checkin.pm_confirmed_at = now
        checkin.pm_confirmed_by = current_user.id
    db.commit()
    return ConfirmResult(confirmed=len(to_confirm))
