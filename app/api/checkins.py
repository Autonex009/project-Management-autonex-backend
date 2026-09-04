"""Daily check-in/check-out API — attendance mode + today's project(s) + mood."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, List
from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.allocation import Allocation
from app.models.project import Project
from app.models.parent_project import MainProject
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
    PaginatedTeamCheckIns,
    ConfirmResult,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/checkins", tags=["checkins"], dependencies=[Depends(get_current_user)])

IST = ZoneInfo("Asia/Kolkata")

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


@router.get("/today", response_model=TodayCheckInStatus)
def get_today_status(
    response: Response,
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
    today = _get_ist_today()

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
        checked_in_at=_get_ist_now(),
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
