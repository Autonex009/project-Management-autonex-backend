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

from datetime import time as dtime, timezone
from sqlalchemy import or_

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

def _apply_time_filter(query, time_filter: str, time_from: str = None, time_to: str = None, today=None):
    """
    Apply check-in time range filters (IST).
    Supported values in time_filter (comma-separated): before_9, 9_10, 10_11, 11_12, custom
    For custom, also pass time_from / time_to as "HH:MM".
    """
    if not time_filter and not (time_from or time_to):
        return query

    today = today or _get_ist_today()
    ranges = []

    values = [v.strip() for v in (time_filter or "").split(",") if v.strip()]

    def make_range(start_h, start_m, end_h=None, end_m=None, inclusive_end=False):
        start = datetime.combine(today, dtime(start_h, start_m), tzinfo=IST).astimezone(timezone.utc)
        if end_h is not None:
            end = datetime.combine(today, dtime(end_h, end_m), tzinfo=IST).astimezone(timezone.utc)
            if inclusive_end:
                return (DailyCheckIn.checked_in_at >= start) & (DailyCheckIn.checked_in_at <= end)
            return (DailyCheckIn.checked_in_at >= start) & (DailyCheckIn.checked_in_at < end)
        return DailyCheckIn.checked_in_at < start

    for v in values:
        if v == "before_9":
            ranges.append(make_range(9, 0))
        elif v == "9_10":
            ranges.append(make_range(9, 0, 10, 0))
        elif v == "10_11":
            ranges.append(make_range(10, 0, 11, 0))
        elif v == "11_12":
            ranges.append(make_range(11, 0, 12, 0))
        elif v == "custom" and time_from and time_to:
            try:
                fh, fm = map(int, time_from.split(":"))
                th, tm = map(int, time_to.split(":"))
                ranges.append(make_range(fh, fm, th, tm, inclusive_end=True))
            except Exception:
                pass  # ignore invalid custom range

    # also allow pure custom without the keyword
    if not values and time_from and time_to:
        try:
            fh, fm = map(int, time_from.split(":"))
            th, tm = map(int, time_to.split(":"))
            ranges.append(make_range(fh, fm, th, tm, inclusive_end=True))
        except Exception:
            pass

    if ranges:
        query = query.filter(or_(*ranges))
        # only people who actually checked in can match a time range
        query = query.filter(DailyCheckIn.checked_in_at.isnot(None))

    return query


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
            kpi_total=kpis.get("total", 0), 
            kpi_checked_in=kpis.get("checked_in", 0), 
            kpi_wfo=kpis.get("wfo", 0),
            kpi_wfh=kpis.get("wfh", 0),
            kpi_confirmed=kpis.get("confirmed", 0),
            kpi_late=kpis.get("late", 0),
            kpi_checked_out=kpis.get("checked_out", 0),
            kpi_floor_7=kpis.get("floor_7", 0),
            kpi_floor_9=kpis.get("floor_9", 0),
            kpi_floor_17=kpis.get("floor_17", 0),
            kpi_order_tiffin=kpis.get("order_tiffin", 0),
            kpi_canteen=kpis.get("canteen", 0),
            kpi_mood_great=kpis.get("mood_great", 0),
            kpi_mood_okay=kpis.get("mood_okay", 0),
            kpi_mood_low=kpis.get("mood_low", 0),
            kpi_mood_stressed=kpis.get("mood_stressed", 0),
            kpi_approved_leaves_count=kpis.get("approved_leaves_count", 0),
            kpi_pending_leaves_count=kpis.get("pending_leaves_count", 0),
            kpi_approved_leaves_names=kpis.get("approved_leaves_names", []),
            kpi_pending_leaves_names=kpis.get("pending_leaves_names", []),
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
    
    from app.models.leave import Leave
    today = _get_ist_today()
    on_leave_ids_query = db.query(Leave.employee_id).filter(
        Leave.employee_id.in_(emp_ids),
        Leave.start_date <= today,
        Leave.end_date >= today,
        Leave.status != "rejected"
    ).all()
    on_leave_set = {r[0] for r in on_leave_ids_query}
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
            office_floor=chk.office_floor if chk else None,
            lunch_preference=chk.lunch_preference if chk else None,
            tiffin_type=chk.tiffin_type if chk else None,
            checked_in_at=chk.checked_in_at if chk else None,
            checked_out_at=chk.checked_out_at if chk else None,
            pm_confirmed_at=chk.pm_confirmed_at if chk else None,
            is_officially_allocated=is_officially_allocated,
            is_on_leave=emp.id in on_leave_set,
        ))
    t5 = time.time()
    print(f"PROFILE build: count={t1-t0:.3f}s results={t2-t1:.3f}s allocs={t3-t2:.3f}s all_proj={t4-t3:.3f}s loop={t5-t4:.3f}s")
        
    return PaginatedTeamCheckIns(
        total=total, page=page, limit=limit, items=items,
        kpi_total=kpis.get("total", 0), 
        kpi_checked_in=kpis.get("checked_in", 0), 
        kpi_wfo=kpis.get("wfo", 0),
        kpi_wfh=kpis.get("wfh", 0),
        kpi_confirmed=kpis.get("confirmed", 0),
        kpi_late=kpis.get("late", 0),
        kpi_checked_out=kpis.get("checked_out", 0),
        kpi_floor_7=kpis.get("floor_7", 0),
        kpi_floor_9=kpis.get("floor_9", 0),
        kpi_floor_17=kpis.get("floor_17", 0),
        kpi_order_tiffin=kpis.get("order_tiffin", 0),
        kpi_canteen=kpis.get("canteen", 0),
        kpi_mood_great=kpis.get("mood_great", 0),
        kpi_mood_okay=kpis.get("mood_okay", 0),
        kpi_mood_low=kpis.get("mood_low", 0),
        kpi_mood_stressed=kpis.get("mood_stressed", 0),
        kpi_approved_leaves_count=kpis.get("approved_leaves_count", 0),
        kpi_pending_leaves_count=kpis.get("pending_leaves_count", 0),
        kpi_approved_leaves_names=kpis.get("approved_leaves_names", []),
        kpi_pending_leaves_names=kpis.get("pending_leaves_names", []),
    )


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
        office_floor=payload.office_floor,
        lunch_preference=payload.lunch_preference,
        tiffin_type=payload.tiffin_type,
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

@router.get("/team-today", response_model=PaginatedTeamCheckIns)
def get_team_today(
    page: int = 1,
    limit: int = 50,
    search: str = "",
    status: str = "",
    work_mode: str = "",
    office_floor: str = "",
    project_id: str = "",
    time_filter: str = "",
    time_from: str = None,          
    time_to: str = None,           
    sentiment: str = "",
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

    all_today_checkins = db.query(
        DailyCheckIn.employee_id, 
        DailyCheckIn.project_ids, 
        DailyCheckIn.pm_confirmed_at, 
        DailyCheckIn.work_mode,
        DailyCheckIn.checked_in_at,
        DailyCheckIn.checked_out_at,
        DailyCheckIn.office_floor,
        DailyCheckIn.lunch_preference,
        DailyCheckIn.mood
    ).filter(DailyCheckIn.checkin_date == today).all()
    checked_in_emp_ids = set()
    for c in all_today_checkins:
        if set(c.project_ids or []).intersection(scoped_project_ids):
            checked_in_emp_ids.add(c.employee_id)
            
    visible_emp_ids = allocated_emp_ids.union(checked_in_emp_ids)
    t2 = time.time()
    
    if not visible_emp_ids:
        return PaginatedTeamCheckIns(total=0, page=page, limit=limit, items=[])

    from datetime import time as dtime
    from datetime import timezone
    late_threshold_ist = datetime.combine(today, dtime(10, 0), tzinfo=IST)
    late_threshold_utc = late_threshold_ist.astimezone(timezone.utc)
    
    from app.models.leave import Leave
    all_leaves = db.query(Leave.employee_id, Leave.status, Employee.name).join(Employee, Leave.employee_id == Employee.id).filter(
        Leave.employee_id.in_(list(visible_emp_ids)),
        Leave.start_date <= today,
        Leave.end_date >= today
    ).all() if visible_emp_ids else []
    
    approved_leaves = [{"id": r[0], "name": r[2]} for r in all_leaves if r[1] == "approved"]
    pending_leaves = [{"id": r[0], "name": r[2]} for r in all_leaves if r[1] == "pending"]

    kpis = {
        "total": len(visible_emp_ids),
        "checked_in": len(checked_in_emp_ids.intersection(visible_emp_ids)),
        "wfo": sum(1 for c in all_today_checkins if c.employee_id in visible_emp_ids and c.work_mode == "WFO"),
        "wfh": sum(1 for c in all_today_checkins if c.employee_id in visible_emp_ids and c.work_mode == "WFH"),
        "confirmed": sum(1 for c in all_today_checkins if c.employee_id in visible_emp_ids and c.pm_confirmed_at is not None),
        "late": sum(1 for c in all_today_checkins if c.employee_id in visible_emp_ids and c.checked_in_at and c.checked_in_at > late_threshold_utc),
        "checked_out": sum(1 for c in all_today_checkins if c.employee_id in visible_emp_ids and c.checked_out_at is not None),
        "floor_7": sum(1 for c in all_today_checkins if c.employee_id in visible_emp_ids and c.office_floor == "7"),
        "floor_9": sum(1 for c in all_today_checkins if c.employee_id in visible_emp_ids and c.office_floor == "9"),
        "floor_17": sum(1 for c in all_today_checkins if c.employee_id in visible_emp_ids and c.office_floor == "17"),
        "order_tiffin": sum(1 for c in all_today_checkins if c.employee_id in visible_emp_ids and c.lunch_preference == "order_tiffin"),
        "canteen": sum(1 for c in all_today_checkins if c.employee_id in visible_emp_ids and c.lunch_preference == "canteen"),
        "mood_great": sum(1 for c in all_today_checkins if c.employee_id in visible_emp_ids and c.mood == "great"),
        "mood_okay": sum(1 for c in all_today_checkins if c.employee_id in visible_emp_ids and c.mood == "okay"),
        "mood_low": sum(1 for c in all_today_checkins if c.employee_id in visible_emp_ids and c.mood == "low"),
        "mood_stressed": sum(1 for c in all_today_checkins if c.employee_id in visible_emp_ids and c.mood == "stressed"),
        "approved_leaves_count": len(approved_leaves),
        "pending_leaves_count": len(pending_leaves),
        "approved_leaves_names": [l["name"] for l in approved_leaves],
        "pending_leaves_names": [l["name"] for l in pending_leaves],
    }
    t3 = time.time()
        
    query = db.query(Employee, DailyCheckIn).outerjoin(
        DailyCheckIn, 
        (Employee.id == DailyCheckIn.employee_id) & (DailyCheckIn.checkin_date == today)
    ).filter(Employee.id.in_(visible_emp_ids))
    
    if search:
        query = query.filter(Employee.name.ilike(f"%{search}%"))
    if status:
        statuses = [s.strip() for s in status.split(",")]
        conds = []
        if "checked_in" in statuses:
            conds.append(DailyCheckIn.id.isnot(None))
        if "pending" in statuses:
            conds.append(DailyCheckIn.id.is_(None))
        if conds:
            from sqlalchemy import or_
            query = query.filter(or_(*conds))
    if work_mode:
        modes = [m.strip() for m in work_mode.split(",")]
        query = query.filter(DailyCheckIn.work_mode.in_(modes))
    if office_floor:
        floors = [f.strip() for f in office_floor.split(",")]
        query = query.filter(DailyCheckIn.office_floor.in_(floors))
    if sentiment:
        sentiments = [s.strip() for s in sentiment.split(",")]
        query = query.filter(DailyCheckIn.mood.in_(sentiments))
        
    if project_id:
        pids = [int(p.strip()) for p in project_id.split(",") if p.strip().isdigit()]
        if pids:
            allocs_proj = db.query(Allocation.employee_id).filter(
                Allocation.sub_project_id.in_(pids), 
                Allocation.is_active == True
            ).all()
            proj_emp_ids = {a.employee_id for a in allocs_proj if a.employee_id}
            from sqlalchemy import or_
            chk_proj_conds = []
            for pid in pids:
                chk_proj_conds.append(DailyCheckIn.project_ids.contains([pid]))
                chk_proj_conds.append(DailyCheckIn.project_ids.contains([str(pid)]))
            chk_proj = db.query(DailyCheckIn.employee_id).filter(
                DailyCheckIn.checkin_date == today,
                or_(*chk_proj_conds)
            ).all()
            proj_emp_ids.update({c.employee_id for c in chk_proj})
            query = query.filter(Employee.id.in_(list(proj_emp_ids) if proj_emp_ids else [-1]))
        
    # if time_filter == "late":
    #     from datetime import time as dtime
    #     from datetime import timezone
    #     late_threshold_ist = datetime.combine(today, dtime(10, 0), tzinfo=IST)
    #     late_threshold_utc = late_threshold_ist.astimezone(timezone.utc)
    #     query = query.filter(DailyCheckIn.checked_in_at > late_threshold_utc)

    query = _apply_time_filter(query, time_filter, time_from, time_to, today)
        
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
    office_floor: str = "",
    project_id: str = "",
    time_filter: str = "",
    time_from: str = None,          
    time_to: str = None,            
    sentiment: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "hr")),
):
    today = _get_ist_today()
    
    total_active = db.query(Employee).filter(Employee.status == "active").count()
    
    checked_in_records = db.query(
        DailyCheckIn.employee_id, 
        DailyCheckIn.work_mode,
        DailyCheckIn.checked_in_at,
        DailyCheckIn.checked_out_at,
        DailyCheckIn.office_floor,
        DailyCheckIn.lunch_preference,
        DailyCheckIn.mood
    ).join(Employee, DailyCheckIn.employee_id == Employee.id).filter(Employee.status == "active", DailyCheckIn.checkin_date == today).all()
    checked_in_active = len(checked_in_records)
    wfo_active = sum(1 for c in checked_in_records if c.work_mode == "WFO")
    wfh_active = sum(1 for c in checked_in_records if c.work_mode == "WFH")
    
    confirmed_active = db.query(DailyCheckIn.employee_id).join(Employee, DailyCheckIn.employee_id == Employee.id).filter(Employee.status == "active", DailyCheckIn.checkin_date == today, DailyCheckIn.pm_confirmed_at.isnot(None)).count()
    
    from datetime import time as dtime
    from datetime import timezone
    late_threshold_ist = datetime.combine(today, dtime(10, 0), tzinfo=IST)
    late_threshold_utc = late_threshold_ist.astimezone(timezone.utc)

    from app.models.leave import Leave
    all_leaves = db.query(Leave.employee_id, Leave.status, Employee.name).join(Employee, Leave.employee_id == Employee.id).filter(
        Employee.status == "active",
        Leave.start_date <= today,
        Leave.end_date >= today
    ).all()
    
    approved_leaves = [{"id": r[0], "name": r[2]} for r in all_leaves if r[1] == "approved"]
    pending_leaves = [{"id": r[0], "name": r[2]} for r in all_leaves if r[1] == "pending"]

    kpis = {
        "total": total_active,
        "checked_in": checked_in_active,
        "wfo": wfo_active,
        "wfh": wfh_active,
        "confirmed": confirmed_active,
        "late": sum(1 for c in checked_in_records if c.checked_in_at and c.checked_in_at > late_threshold_utc),
        "checked_out": sum(1 for c in checked_in_records if c.checked_out_at is not None),
        "floor_7": sum(1 for c in checked_in_records if c.office_floor == "7"),
        "floor_9": sum(1 for c in checked_in_records if c.office_floor == "9"),
        "floor_17": sum(1 for c in checked_in_records if c.office_floor == "17"),
        "order_tiffin": sum(1 for c in checked_in_records if c.lunch_preference == "order_tiffin"),
        "canteen": sum(1 for c in checked_in_records if c.lunch_preference == "canteen"),
        "mood_great": sum(1 for c in checked_in_records if c.mood == "great"),
        "mood_okay": sum(1 for c in checked_in_records if c.mood == "okay"),
        "mood_low": sum(1 for c in checked_in_records if c.mood == "low"),
        "mood_stressed": sum(1 for c in checked_in_records if c.mood == "stressed"),
        "approved_leaves_count": len(approved_leaves),
        "pending_leaves_count": len(pending_leaves),
        "approved_leaves_names": [l["name"] for l in approved_leaves],
        "pending_leaves_names": [l["name"] for l in pending_leaves],
    }
    
    query = db.query(Employee, DailyCheckIn).outerjoin(
        DailyCheckIn, 
        (Employee.id == DailyCheckIn.employee_id) & (DailyCheckIn.checkin_date == today)
    ).filter(Employee.status == "active")
    
    if search:
        query = query.filter(Employee.name.ilike(f"%{search}%"))
    if status:
        statuses = [s.strip() for s in status.split(",")]
        conds = []
        if "checked_in" in statuses:
            conds.append(DailyCheckIn.id.isnot(None))
        if "pending" in statuses:
            conds.append(DailyCheckIn.id.is_(None))
        if conds:
            from sqlalchemy import or_
            query = query.filter(or_(*conds))
    if work_mode:
        modes = [m.strip() for m in work_mode.split(",")]
        query = query.filter(DailyCheckIn.work_mode.in_(modes))
    if office_floor:
        floors = [f.strip() for f in office_floor.split(",")]
        query = query.filter(DailyCheckIn.office_floor.in_(floors))
    if sentiment:
        sentiments = [s.strip() for s in sentiment.split(",")]
        query = query.filter(DailyCheckIn.mood.in_(sentiments))
        
    if project_id:
        project_id_parts = [p.strip() for p in project_id.split(",") if p.strip()]
        has_unassigned = "unassigned" in project_id_parts or "idle" in project_id_parts
        pids = [int(p) for p in project_id_parts if p.isdigit()]
        
        proj_emp_ids = set()
        if pids:
            allocs_proj = db.query(Allocation.employee_id).filter(
                Allocation.sub_project_id.in_(pids), 
                Allocation.is_active == True
            ).all()
            proj_emp_ids.update({a.employee_id for a in allocs_proj if a.employee_id})
            from sqlalchemy import or_
            chk_proj_conds = []
            for pid in pids:
                chk_proj_conds.append(DailyCheckIn.project_ids.contains([pid]))
                chk_proj_conds.append(DailyCheckIn.project_ids.contains([str(pid)]))
            chk_proj = db.query(DailyCheckIn.employee_id).filter(
                DailyCheckIn.checkin_date == today,
                or_(*chk_proj_conds)
            ).all()
            proj_emp_ids.update({c.employee_id for c in chk_proj})

        if has_unassigned:
            allocated_emp_ids = {a.employee_id for a in db.query(Allocation.employee_id).filter(Allocation.is_active == True).all() if a.employee_id}
            all_active = {e.id for e in db.query(Employee.id).filter(Employee.status == "active").all()}
            unassigned_ids = all_active.difference(allocated_emp_ids)
            proj_emp_ids.update(unassigned_ids)

        if proj_emp_ids or pids or has_unassigned:
            query = query.filter(Employee.id.in_(list(proj_emp_ids) if proj_emp_ids else [-1]))
        
    # if time_filter == "late":
    #     from datetime import time as dtime
    #     from datetime import timezone
    #     late_threshold_ist = datetime.combine(today, dtime(10, 0), tzinfo=IST)
    #     late_threshold_utc = late_threshold_ist.astimezone(timezone.utc)
    #     query = query.filter(DailyCheckIn.checked_in_at > late_threshold_utc)

    query = _apply_time_filter(query, time_filter, time_from, time_to, today)
        
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


from datetime import timedelta

@router.get("/admin/sentiment-analytics")
def get_admin_sentiment_analytics(
    target_date: Optional[str] = None,
    date_range: Optional[str] = "today",
    project_id: Optional[str] = None,
    work_mode: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "hr")),
):
    """
    Detailed Mood/Sentiment analytics for Admin across Daily, Weekly, and Monthly views,
    with project/work_mode filtering, date range presets, employee mood lists, and executive summary.
    """
    if target_date:
        try:
            t_date = datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError:
            t_date = _get_ist_today()
    else:
        t_date = _get_ist_today()

    active_emp_ids = {e.id for e in db.query(Employee.id).filter(Employee.status == "active").all()}

    # Project filtering if requested
    if project_id:
        proj_parts = [p.strip() for p in project_id.split(",") if p.strip()]
        has_unassigned = "unassigned" in proj_parts or "idle" in proj_parts
        pids = [int(p) for p in proj_parts if p.isdigit()]
        
        target_emp_ids = set()
        if pids:
            allocs_proj = db.query(Allocation.employee_id).filter(
                Allocation.sub_project_id.in_(pids),
                Allocation.is_active == True
            ).all()
            target_emp_ids.update({a.employee_id for a in allocs_proj if a.employee_id})
            
            from sqlalchemy import or_
            chk_proj_conds = []
            for pid in pids:
                chk_proj_conds.append(DailyCheckIn.project_ids.contains([pid]))
                chk_proj_conds.append(DailyCheckIn.project_ids.contains([str(pid)]))
            
            chk_proj = db.query(DailyCheckIn.employee_id).filter(
                DailyCheckIn.checkin_date == t_date,
                or_(*chk_proj_conds)
            ).all()
            target_emp_ids.update({c.employee_id for c in chk_proj})

        if has_unassigned:
            allocated_emp_ids = {a.employee_id for a in db.query(Allocation.employee_id).filter(Allocation.is_active == True).all() if a.employee_id}
            unassigned_emp_ids = active_emp_ids.difference(allocated_emp_ids)
            target_emp_ids.update(unassigned_emp_ids)

        if target_emp_ids or pids or has_unassigned:
            active_emp_ids = active_emp_ids.intersection(target_emp_ids)

    # Work mode filtering
    work_modes_list = [m.strip() for m in work_mode.split(",") if m.strip()] if work_mode else []

    # Map employee details and project names
    emp_objects = {e.id: e for e in db.query(Employee).filter(Employee.id.in_(active_emp_ids)).all()}
    all_projects = {p.id: p.name for p in db.query(Project.id, Project.name).all()}
    
    allocs = db.query(Allocation.employee_id, Project.name).join(Project, Allocation.sub_project_id == Project.id).filter(
        Allocation.employee_id.in_(active_emp_ids), Allocation.is_active == True
    ).all()
    emp_alloc_projects = {}
    for eid, pname in allocs:
        emp_alloc_projects.setdefault(eid, set()).add(pname)

    # Base query helper
    def build_checkin_query(start_d, end_d):
        q = db.query(DailyCheckIn).filter(
            DailyCheckIn.checkin_date >= start_d,
            DailyCheckIn.checkin_date <= end_d,
            DailyCheckIn.employee_id.in_(active_emp_ids),
            DailyCheckIn.mood.isnot(None)
        )
        if work_modes_list:
            q = q.filter(DailyCheckIn.work_mode.in_(work_modes_list))
        return q

    # 1. DAILY ANALYTICS (for t_date)
    daily_records = build_checkin_query(t_date, t_date).all()

    daily_dist = {"great": 0, "okay": 0, "low": 0, "stressed": 0}
    daily_wfo = {"great": 0, "okay": 0, "low": 0, "stressed": 0}
    daily_wfh = {"great": 0, "okay": 0, "low": 0, "stressed": 0}
    employees_by_mood = {"great": [], "okay": [], "low": [], "stressed": []}

    for r in daily_records:
        m = (r.mood or "").lower()
        if m in daily_dist:
            daily_dist[m] += 1
            if r.work_mode == "WFO":
                daily_wfo[m] += 1
            elif r.work_mode == "WFH":
                daily_wfh[m] += 1

            if r.employee_id in emp_objects:
                emp = emp_objects[r.employee_id]
                alloc_pnames = list(emp_alloc_projects.get(emp.id, set()))
                chk_pnames = []
                if r.project_ids:
                    chk_pnames = [all_projects[pid] for pid in r.project_ids if pid in all_projects]
                    if "other" in r.project_ids:
                        chk_pnames.append("Other")
                pnames = list(set(alloc_pnames + chk_pnames))
                if not pnames:
                    pnames = ["Idle / Unassigned"]

                employees_by_mood[m].append({
                    "employee_id": emp.id,
                    "name": emp.name,
                    "avatar_url": getattr(emp, "avatar_url", None),
                    "designation": emp.designation,
                    "work_mode": r.work_mode,
                    "project_names": pnames
                })

    daily_total = sum(daily_dist.values())
    daily_positive = daily_dist["great"] + daily_dist["okay"]
    daily_positivity_index = round((daily_positive / daily_total * 100), 1) if daily_total > 0 else 0.0

    # Daily positivity delta vs yesterday
    yesterday = t_date - timedelta(days=1)
    yesterday_records = build_checkin_query(yesterday, yesterday).all()
    prev_tot = len(yesterday_records)
    prev_pos = sum(1 for r in yesterday_records if (r.mood or "").lower() in ["great", "okay"])
    prev_positivity = round((prev_pos / prev_tot * 100), 1) if prev_tot > 0 else 0.0
    daily_delta = round(daily_positivity_index - prev_positivity, 1)

    # 2. WEEKLY ANALYTICS (7 days ending on t_date)
    week_start = t_date - timedelta(days=6)
    weekly_records = build_checkin_query(week_start, t_date).all()

    weekly_dist = {"great": 0, "okay": 0, "low": 0, "stressed": 0}
    weekly_by_date = {}

    for i in range(7):
        d = week_start + timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        day_name = d.strftime("%a")
        weekly_by_date[d_str] = {
            "date": d_str,
            "label": d.strftime("%d %b (%a)"),
            "day_name": day_name,
            "great": 0,
            "okay": 0,
            "low": 0,
            "stressed": 0,
            "total": 0,
            "positivity_index": 0.0
        }

    for r in weekly_records:
        m = (r.mood or "").lower()
        if m in weekly_dist:
            weekly_dist[m] += 1
            d_str = r.checkin_date.strftime("%Y-%m-%d")
            if d_str in weekly_by_date:
                weekly_by_date[d_str][m] += 1
                weekly_by_date[d_str]["total"] += 1

    for d_str, item in weekly_by_date.items():
        tot = item["total"]
        pos = item["great"] + item["okay"]
        item["positivity_index"] = round((pos / tot * 100), 1) if tot > 0 else 0.0

    weekly_total = sum(weekly_dist.values())
    weekly_positive = weekly_dist["great"] + weekly_dist["okay"]
    weekly_positivity_index = round((weekly_positive / weekly_total * 100), 1) if weekly_total > 0 else 0.0
    weekly_trends = list(weekly_by_date.values())

    # 3. MONTHLY ANALYTICS (Calendar month of t_date)
    from datetime import date
    _, days_in_month = calendar.monthrange(t_date.year, t_date.month)
    month_start = date(t_date.year, t_date.month, 1)
    month_end = date(t_date.year, t_date.month, days_in_month)

    monthly_records = build_checkin_query(month_start, month_end).all()

    monthly_dist = {"great": 0, "okay": 0, "low": 0, "stressed": 0}
    monthly_by_date = {}

    for day_num in range(1, days_in_month + 1):
        d = date(t_date.year, t_date.month, day_num)
        d_str = d.strftime("%Y-%m-%d")
        monthly_by_date[d_str] = {
            "date": d_str,
            "day": str(day_num),
            "label": d.strftime("%d %b"),
            "great": 0,
            "okay": 0,
            "low": 0,
            "stressed": 0,
            "total": 0,
            "positivity_index": 0.0
        }

    for r in monthly_records:
        m = (r.mood or "").lower()
        if m in monthly_dist:
            monthly_dist[m] += 1
            d_str = r.checkin_date.strftime("%Y-%m-%d")
            if d_str in monthly_by_date:
                monthly_by_date[d_str][m] += 1
                monthly_by_date[d_str]["total"] += 1

    for d_str, item in monthly_by_date.items():
        tot = item["total"]
        pos = item["great"] + item["okay"]
        item["positivity_index"] = round((pos / tot * 100), 1) if tot > 0 else 0.0

    monthly_total = sum(monthly_dist.values())
    monthly_positive = monthly_dist["great"] + monthly_dist["okay"]
    monthly_positivity_index = round((monthly_positive / monthly_total * 100), 1) if monthly_total > 0 else 0.0
    monthly_trends = list(monthly_by_date.values())

    # 4. ANNUAL / YEARLY ANALYTICS (Calendar year of t_date)
    year_start = date(t_date.year, 1, 1)
    year_end = date(t_date.year, 12, 31)
    yearly_records = build_checkin_query(year_start, year_end).all()

    yearly_dist = {"great": 0, "okay": 0, "low": 0, "stressed": 0}
    yearly_by_month = {}

    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for m_idx in range(1, 13):
        m_key = f"{t_date.year}-{m_idx:02d}"
        yearly_by_month[m_key] = {
            "date": m_key,
            "day": month_names[m_idx - 1],
            "label": f"{month_names[m_idx - 1]} {t_date.year}",
            "great": 0,
            "okay": 0,
            "low": 0,
            "stressed": 0,
            "total": 0,
            "positivity_index": 0.0
        }

    for r in yearly_records:
        m = (r.mood or "").lower()
        if m in yearly_dist:
            yearly_dist[m] += 1
            m_key = r.checkin_date.strftime("%Y-%m")
            if m_key in yearly_by_month:
                yearly_by_month[m_key][m] += 1
                yearly_by_month[m_key]["total"] += 1

    for m_key, item in yearly_by_month.items():
        tot = item["total"]
        pos = item["great"] + item["okay"]
        item["positivity_index"] = round((pos / tot * 100), 1) if tot > 0 else 0.0

    yearly_total = sum(yearly_dist.values())
    yearly_positive = yearly_dist["great"] + yearly_dist["okay"]
    yearly_positivity_index = round((yearly_positive / yearly_total * 100), 1) if yearly_total > 0 else 0.0
    yearly_trends = list(yearly_by_month.values())

    # 5. PROJECT-WISE BREAKDOWN
    proj_map_stats = {}
    for r in daily_records:
        m = (r.mood or "").lower()
        if m not in ["great", "okay", "low", "stressed"]:
            continue
        pids = set()
        if r.project_ids:
            pids = {pid for pid in r.project_ids if isinstance(pid, int) and pid in all_projects}
        if r.employee_id in emp_alloc_projects:
            for p_id, p_name in all_projects.items():
                if p_name in emp_alloc_projects[r.employee_id]:
                    pids.add(p_id)

        if not pids:
            pid = "unassigned"
            if pid not in proj_map_stats:
                proj_map_stats[pid] = {
                    "project_id": "unassigned",
                    "project_name": "Idle / Unassigned",
                    "great": 0, "okay": 0, "low": 0, "stressed": 0, "total": 0
                }
            proj_map_stats[pid][m] += 1
            proj_map_stats[pid]["total"] += 1
        else:
            for pid in pids:
                if pid not in proj_map_stats:
                    proj_map_stats[pid] = {
                        "project_id": pid,
                        "project_name": all_projects[pid],
                        "great": 0, "okay": 0, "low": 0, "stressed": 0, "total": 0
                    }
                proj_map_stats[pid][m] += 1
                proj_map_stats[pid]["total"] += 1

    project_breakdown = []
    for pid, pstat in proj_map_stats.items():
        tot = pstat["total"]
        pos = pstat["great"] + pstat["okay"]
        pstat["positivity_index"] = round((pos / tot * 100), 1) if tot > 0 else 0.0
        if pstat["positivity_index"] >= 75:
            pstat["health_status"] = "Healthy"
            pstat["health_badge"] = "🟢 Healthy"
        elif pstat["positivity_index"] >= 50:
            pstat["health_status"] = "Moderate"
            pstat["health_badge"] = "🟡 Moderate"
        else:
            pstat["health_status"] = "At-Risk"
            pstat["health_badge"] = "🔴 At-Risk"
        project_breakdown.append(pstat)
    project_breakdown.sort(key=lambda x: x["positivity_index"], reverse=True)

    def get_dominant_sentiment(dist):
        if not dist or max(dist.values()) == 0:
            return "None"
        best = max(dist.items(), key=lambda x: x[1])[0]
        return best.capitalize()

    # Executive Summary Text
    at_risk_count = daily_dist["low"] + daily_dist["stressed"]
    summary_text = (
        f"On {t_date.strftime('%d %b %Y')}, {daily_positivity_index}% of check-in responses were positive (Great/Okay). "
        f"Dominant vibe is '{get_dominant_sentiment(daily_dist)}'. "
    )
    if at_risk_count > 0:
        summary_text += f"⚠️ {at_risk_count} employee{'s' if at_risk_count > 1 else ''} reported Low or Stressed mood today — management review recommended."
    else:
        summary_text += "✨ No high-stress alerts recorded today."

    return {
        "target_date": t_date.strftime("%Y-%m-%d"),
        "month_year": t_date.strftime("%B %Y"),
        "year": str(t_date.year),
        "executive_summary": summary_text,
        "daily": {
            "date": t_date.strftime("%Y-%m-%d"),
            "total_responses": daily_total,
            "positivity_index": daily_positivity_index,
            "positivity_delta": daily_delta,
            "distribution": daily_dist,
            "dominant_sentiment": get_dominant_sentiment(daily_dist),
            "by_work_mode": {
                "wfo": daily_wfo,
                "wfh": daily_wfh
            },
            "employees_by_mood": employees_by_mood,
            "project_breakdown": project_breakdown
        },
        "weekly": {
            "start_date": week_start.strftime("%Y-%m-%d"),
            "end_date": t_date.strftime("%Y-%m-%d"),
            "total_responses": weekly_total,
            "positivity_index": weekly_positivity_index,
            "distribution": weekly_dist,
            "dominant_sentiment": get_dominant_sentiment(weekly_dist),
            "trends": weekly_trends
        },
        "monthly": {
            "month_year": t_date.strftime("%B %Y"),
            "total_responses": monthly_total,
            "positivity_index": monthly_positivity_index,
            "distribution": monthly_dist,
            "dominant_sentiment": get_dominant_sentiment(monthly_dist),
            "trends": monthly_trends
        },
        "yearly": {
            "year": str(t_date.year),
            "month_year": f"Year {t_date.year}",
            "total_responses": yearly_total,
            "positivity_index": yearly_positivity_index,
            "distribution": yearly_dist,
            "dominant_sentiment": get_dominant_sentiment(yearly_dist),
            "trends": yearly_trends
        }
    }
