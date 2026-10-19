"""
Project-based monthly performance evaluations.

Flow:
- The five parameters are fixed (app/constants/perf_params.py) — no PM setup.
- Employee submits a monthly evaluation for a project, rating each parameter 1-5
  (POST). Locked after submit.
- PM reviews (PATCH /{id}/review): approves/rejects each parameter, assigns their
  own 1-5 rating, leaves feedback on rejected ones, and may suggest a bonus.
  PM ratings drive overall_rating and status becomes "reviewed".
- Admin views all evaluations (GET, optional status filter).
- GET /dashboard: pre-scoped, pre-aggregated view for the Performance Reviews page —
  see the scoping helpers below.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session
from app.services.auth_service import get_current_user, has_team_read, require_role
from app.services import project_scope
from app.services.perf_cache import perf_cache
from pydantic import BaseModel, field_validator
from app.models.user import User
from app.models.employee import Employee
from app.models.allocation import Allocation

from app.constants.perf_params import PERF_PARAM_NAME_SET, RATING_MIN, RATING_MAX
from app.db.database import get_db
from app.models.perf_eval import PerfEvaluation
from app.models.notification import Notification
from app.models.project import DailySheet
from app.models.sub_project import SubProject
from app.models.parent_project import MainProject

router = APIRouter(prefix="/api/perf-evals", tags=["Performance Evaluations"], dependencies=[Depends(get_current_user)])

PERIOD_OK = lambda v: isinstance(v, str) and len(v) == 7 and v[4] == "-" and v[:4].isdigit() and v[5:].isdigit() and 1 <= int(v[5:]) <= 12


def _period_label(period: str) -> str:
    try:
        y, m = period.split("-")
        months = ["", "January", "February", "March", "April", "May", "June",
                  "July", "August", "September", "October", "November", "December"]
        return f"{months[int(m)]} {y}"
    except Exception:
        return period


def _notify_on_submit(db: Session, ev: PerfEvaluation) -> None:
    """Notify the right reviewers when an evaluation is submitted.

    - PM self-report (project_id == 0) → notify all admins.
    - Employee evaluation → notify the PM(s) of the project's parent; if none,
      fall back to admins.
    """
    employee = db.query(Employee).filter(Employee.id == ev.employee_id).first()
    who = employee.name if employee else f"Employee #{ev.employee_id}"
    label = _period_label(ev.period)

    def push(user_id: int, title: str, message: str, notif_type: str):
        db.add(Notification(user_id=user_id, title=title, message=message, type=notif_type))

    if ev.project_id == 0:
        # PM self-report → all active admins
        from app.models.user import User as _User
        for admin_user in db.query(_User).filter(_User.role == "admin", _User.is_active == True).all():
            push(admin_user.id,
                 f"PM self-evaluation submitted",
                 f"{who} submitted their {label} self-evaluation for your approval.",
                 "perf_pm_submitted")
        db.commit()
        return

    # Employee evaluation → notify the project's PM(s)
    from app.models.user import User as _User
    pm_employee_ids: set[int] = set()
    sheet = db.query(DailySheet).filter(DailySheet.id == ev.project_id).first()
    main_project_id = getattr(sheet, "main_project_id", None) if sheet else None
    if not main_project_id and sheet and getattr(sheet, "sub_project_id", None):
        sub = db.query(SubProject).filter(SubProject.id == sheet.sub_project_id).first()
        main_project_id = getattr(sub, "main_project_id", None) if sub else None
    if main_project_id:
        mp = db.query(MainProject).filter(MainProject.id == main_project_id).first()
        if mp:
            if getattr(mp, "program_manager_ids", None):
                pm_employee_ids.update([pid for pid in mp.program_manager_ids if pid])
            if getattr(mp, "program_manager_id", None):
                pm_employee_ids.add(mp.program_manager_id)

    project_name = getattr(sheet, "name", None) or "a project"
    notified = False
    for emp_id in pm_employee_ids:
        pm_user = db.query(_User).filter(_User.employee_id == emp_id, _User.is_active == True).first()
        if pm_user:
            notified = True
            push(pm_user.id,
                 "New self-evaluation submitted",
                 f"{who} submitted a {label} self-evaluation for “{project_name}”.",
                 "perf_submitted")

    if not notified:
        for admin_user in db.query(_User).filter(_User.role == "admin", _User.is_active == True).all():
            push(admin_user.id,
                 "New self-evaluation submitted",
                 f"{who} submitted a {label} self-evaluation for “{project_name}” (no PM assigned).",
                 "perf_submitted")
    db.commit()


def _mean(values: List[float]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


# ── Dashboard scoping — mirrors frontend/src/utils/pmScope.js and roleAccess.js ─────────
# Ported 1:1 rather than reused from app/services/project_scope.py: the Performance
# Reviews page has always used the pmScope.js algorithm (assigned_employee_ids /
# allocation / org-fallback), which is a DIFFERENT algorithm from
# project_scope.can_act_on_project (PM + lead-tag based, used by team-kpi/team-data).
# Keep these two helpers in step with pmScope.js and roleAccess.js, not with
# project_scope.py — the two scoping systems are not currently reconciled.

_ADMIN_ONLY_SUBJECT_DESIGNATIONS_UI = {"program manager", "project manager", "hr"}
_FULL_ACCESS_ROLES_UI = {"admin", "hr"}


def _normalise_designation(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _subject_is_admin_only(employee: Optional[Employee]) -> bool:
    return _normalise_designation(getattr(employee, "designation", None)) in _ADMIN_ONLY_SUBJECT_DESIGNATIONS_UI


def _is_team_lead_designation(designation: Optional[str]) -> bool:
    return "team lead" in _normalise_designation(designation)


def _can_decide_for_employee(
    viewer_role: str, viewer_employee_id: Optional[int], employee: Optional[Employee]
) -> bool:
    """Direct port of canDecideForEmployee (roleAccess.js). A tier check only — the
    caller is expected to have already scoped by project, exactly as the frontend does
    (this runs only on evaluations belonging to already-scoped projects)."""
    if viewer_role in _FULL_ACCESS_ROLES_UI:
        return True
    if employee is None:
        return False
    if viewer_employee_id is not None and employee.id == viewer_employee_id:
        return False
    if _subject_is_admin_only(employee):
        return False
    if _is_team_lead_designation(employee.designation):
        return viewer_role == "pm"
    return True


def _get_pm_project_ids(main_projects: List[MainProject], pm_employee_id: Optional[int]) -> set[int]:
    """Direct port of getPmProjectIds (pmScope.js)."""
    if not pm_employee_id:
        return set()
    ids: set[int] = set()
    for mp in main_projects:
        pm_ids = getattr(mp, "program_manager_ids", None) or []
        if getattr(mp, "program_manager_id", None) == pm_employee_id or pm_employee_id in pm_ids:
            ids.add(mp.id)
    return ids


def _get_pm_sub_projects(
    daily_sheets: List[DailySheet],
    main_projects: List[MainProject],
    pm_employee_id: Optional[int],
    allocations: List[Allocation],
) -> List[DailySheet]:
    """Direct port of getPmSubProjects (pmScope.js). ``daily_sheets`` is what the
    frontend calls "sub projects" — PerfEvaluation.project_id points at these rows."""
    if not pm_employee_id:
        return []
    project_ids = _get_pm_project_ids(main_projects, pm_employee_id)
    allocated_ids = {
        a.sub_project_id for a in allocations
        if a.employee_id == pm_employee_id and a.sub_project_id is not None
    }
    result = []
    for sp in daily_sheets:
        project_pms = getattr(sp, "assigned_employee_ids", None) or []
        directly_assigned = pm_employee_id in project_pms
        directly_allocated = sp.id in allocated_ids
        has_project_pm = len(project_pms) > 0
        org_fallback = (
            not has_project_pm
            and getattr(sp, "main_project_id", None)
            and sp.main_project_id in project_ids
        )
        if directly_assigned or directly_allocated or org_fallback:
            result.append(sp)
    return result


# ── Employee submission ──────────────────────────────────────────────────────
class EmployeeParamValue(BaseModel):
    name: str
    employee_rating: int

    @field_validator("employee_rating")
    @classmethod
    def check_rating(cls, v):
        if not (RATING_MIN <= v <= RATING_MAX):
            raise ValueError(f"employee_rating must be between {RATING_MIN} and {RATING_MAX}")
        return v


class PerfEvalCreate(BaseModel):
    project_id: int
    employee_id: int
    period: str
    parameter_values: List[EmployeeParamValue]
    overall_comment: Optional[str] = None

    @field_validator("period")
    @classmethod
    def check_period(cls, v):
        if not PERIOD_OK(v):
            raise ValueError("period must be in YYYY-MM format")
        return v

    @field_validator("parameter_values")
    @classmethod
    def check_params(cls, v):
        names = {p.name for p in v}
        if names != PERF_PARAM_NAME_SET:
            raise ValueError("parameter_values must cover exactly the five fixed parameters")
        return v


# ── PM review ────────────────────────────────────────────────────────────────
class ReviewParamValue(BaseModel):
    name: str
    pm_rating: int
    approved: bool = True
    feedback: Optional[str] = None

    @field_validator("pm_rating")
    @classmethod
    def check_rating(cls, v):
        if not (RATING_MIN <= v <= RATING_MAX):
            raise ValueError(f"pm_rating must be between {RATING_MIN} and {RATING_MAX}")
        return v


class PerfEvalReview(BaseModel):
    parameter_values: List[ReviewParamValue]
    bonus_suggested: bool = False
    bonus_note: Optional[str] = None

    @field_validator("parameter_values")
    @classmethod
    def check_params(cls, v):
        names = {p.name for p in v}
        if names != PERF_PARAM_NAME_SET:
            raise ValueError("parameter_values must cover exactly the five fixed parameters")
        for p in v:
            if not p.approved and not (p.feedback and p.feedback.strip()):
                raise ValueError(f"feedback is required for rejected parameter '{p.name}'")
        return v


class PerfEvalResponse(BaseModel):
    id: int
    project_id: int
    employee_id: int
    period: str
    parameter_values: List[Dict[str, Any]]
    overall_comment: Optional[str] = None
    employee_overall_rating: Optional[float] = None
    overall_rating: Optional[float] = None
    bonus_suggested: bool = False
    bonus_note: Optional[str] = None
    status: str
    submitted_by: Optional[int] = None
    reviewed_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    # Enriched fields for high-performance direct rendering without frontend N+1 API calls:
    employee_name: Optional[str] = None
    employee_avatar_url: Optional[str] = None
    project_name: Optional[str] = None

    class Config:
        from_attributes = True


@router.get("", response_model=dict)
def list_evals(
    project_id: Optional[str] = None,
    employee_id: Optional[int] = None,
    period: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    role_filter: Optional[str] = None,
    pm_id: Optional[str] = None,
    type: Optional[str] = None,  # 'employee' (exclude pm self evals), 'pm' (only pm self evals), 'bonus'
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    page_num = page if isinstance(page, int) else int(getattr(page, "default", 1))
    limit_num = limit if isinstance(limit, int) else int(getattr(limit, "default", 25))

    # ── 1. Dual-Tier Cache Check (L1 RAM -> L2 Redis) ───────────────────
    cache_key = None
    if period:
        cache_filters = {
            "project_id": project_id,
            "employee_id": employee_id,
            "status": status,
            "search": search,
            "role_filter": role_filter,
            "pm_id": pm_id,
            "type": type,
        }
        cache_key = perf_cache.make_key(period, current_user.id, cache_filters, page_num, limit_num)
        cached_result, hit_type = perf_cache.get(cache_key)
        if cached_result is not None:
            return cached_result

    # ── 2. User Access & Permission Verification ────────────────────────
    if not has_team_read(current_user):
        if employee_id is None:
            employee_id = current_user.employee_id
            if employee_id is None:
                emp = db.query(Employee).filter(Employee.email == current_user.email).first()
                if emp:
                    employee_id = emp.id
                else:
                    raise HTTPException(status_code=403, detail="Access denied")
        else:
            is_self = current_user.employee_id == employee_id
            if not is_self:
                emp = db.query(Employee).filter(Employee.id == employee_id).first()
                if not emp or emp.email != current_user.email:
                    raise HTTPException(status_code=403, detail="Access denied")

    # ── 3. Strict Column Projections (skilss.md Law 2) ──────────────────
    # Select only the 15 required evaluation fields plus joined Employee and DailySheet names.
    # Strips out 67 unused columns (PII, encrypted salaries, raw blobs) and eliminates frontend N+1 calls.
    q = db.query(
        PerfEvaluation.id,
        PerfEvaluation.project_id,
        PerfEvaluation.employee_id,
        PerfEvaluation.period,
        PerfEvaluation.parameter_values,
        PerfEvaluation.overall_comment,
        PerfEvaluation.employee_overall_rating,
        PerfEvaluation.overall_rating,
        PerfEvaluation.bonus_suggested,
        PerfEvaluation.bonus_note,
        PerfEvaluation.status,
        PerfEvaluation.submitted_by,
        PerfEvaluation.reviewed_by,
        PerfEvaluation.created_at,
        PerfEvaluation.updated_at,
        Employee.name.label("employee_name"),
        Employee.avatar_url.label("employee_avatar_url"),
        DailySheet.name.label("project_name"),
    ).outerjoin(
        Employee, Employee.id == PerfEvaluation.employee_id
    ).outerjoin(
        DailySheet, DailySheet.id == PerfEvaluation.project_id
    )

    # ── 4. Push Hierarchy Read-Privacy Directly into SQL ────────────────
    # Replaces the catastrophic in-memory N+1 loop with a single index-accelerated WHERE clause.
    if current_user.role in ("pm", "team_lead") and not project_scope.has_full_access(current_user):
        manageable_ids = project_scope.get_manageable_employee_ids(db, current_user)
        privacy_conds = [PerfEvaluation.reviewed_by == current_user.id]
        if current_user.employee_id:
            privacy_conds.append(PerfEvaluation.employee_id == current_user.employee_id)
        if manageable_ids:
            privacy_conds.append(PerfEvaluation.employee_id.in_(manageable_ids))
        q = q.filter(or_(*privacy_conds))

    # ── 5. Standard Filters ─────────────────────────────────────────────
    if project_id and str(project_id) != "all":
        p_ids = [int(p.strip()) for p in str(project_id).split(",") if p.strip().isdigit()]
        if len(p_ids) == 1:
            q = q.filter(PerfEvaluation.project_id == p_ids[0])
        elif len(p_ids) > 1:
            q = q.filter(PerfEvaluation.project_id.in_(p_ids))
    elif pm_id and str(pm_id) != "all":
        pm_ids = [int(p.strip()) for p in str(pm_id).split(",") if p.strip().isdigit()]
        if pm_ids:
            main_projects = db.query(MainProject).all()
            daily_sheets = db.query(DailySheet).all()
            allocations = db.query(Allocation).filter(Allocation.employee_id.in_(pm_ids)).all()
            all_pm_sub_project_ids = set()
            for pid in pm_ids:
                pm_sub_projects = _get_pm_sub_projects(daily_sheets, main_projects, pid, allocations)
                all_pm_sub_project_ids.update([sp.id for sp in pm_sub_projects])
            if not all_pm_sub_project_ids:
                empty_res = {"items": [], "total": 0, "page": page_num, "limit": limit_num}
                if cache_key:
                    perf_cache.set(cache_key, empty_res, is_immutable=True)
                return empty_res
            q = q.filter(PerfEvaluation.project_id.in_(list(all_pm_sub_project_ids)))

    if employee_id:
        q = q.filter(PerfEvaluation.employee_id == employee_id)
    if period:
        q = q.filter(PerfEvaluation.period == period)
    if status and str(status) != "all":
        statuses = [s.strip() for s in str(status).split(",") if s.strip()]
        if len(statuses) == 1:
            q = q.filter(PerfEvaluation.status == statuses[0])
        elif len(statuses) > 1:
            q = q.filter(PerfEvaluation.status.in_(statuses))

    if type == "bonus":
        q = q.filter(PerfEvaluation.bonus_suggested == True)

    if type == "pm":
        q = q.filter(PerfEvaluation.project_id == 0, Employee.designation.ilike("%manager%"))
    elif type == "hr":
        q = q.filter(PerfEvaluation.project_id == 0, Employee.designation.ilike("%hr%"))

    if search and search.strip():
        q = q.filter(Employee.name.ilike(f"%{search.strip()}%"))
    if role_filter and str(role_filter) != "all":
        roles = [r.strip().lower() for r in str(role_filter).split(",") if r.strip()]
        role_conds = []
        for r in roles:
            if r == "admin":
                role_conds.append(Employee.designation.ilike("%admin%"))
            elif r == "pm":
                role_conds.append(Employee.designation.ilike("%manager%"))
            elif r == "team_lead":
                role_conds.append(Employee.designation.ilike("%lead%"))
            elif r == "hr":
                role_conds.append(Employee.designation.ilike("%hr%"))
            elif r == "annotator":
                role_conds.append(Employee.designation.ilike("%annotator%"))
            elif r in ["full-time", "intern"]:
                cond = Employee.employee_type.ilike(r)
                if r == "full-time":
                    cond = and_(cond, ~Employee.designation.ilike("%manager%"), ~Employee.designation.ilike("%hr%"), ~Employee.designation.ilike("%lead%"))
                role_conds.append(cond)
            elif r == "contract":
                role_conds.append(Employee.employee_type.ilike("contract%"))
        if role_conds:
            q = q.filter(or_(*role_conds))

    # ── 6. High-Performance Pagination & Fetch ──────────────────────────
    offset = (page_num - 1) * limit_num
    fetch_limit = limit_num + 1 if page_num == 1 else limit_num
    rows = q.order_by(PerfEvaluation.period.desc(), PerfEvaluation.created_at.desc())\
            .offset(offset)\
            .limit(fetch_limit).all()

    # Fast-path total: If page 1 returns fewer rows than limit, skip count query entirely!
    if page_num == 1 and len(rows) <= limit_num:
        total = len(rows)
        items = rows
    else:
        if page_num == 1:
            items = rows[:limit_num]
        else:
            items = rows
        total = q.with_entities(func.count(PerfEvaluation.id)).order_by(None).scalar() or 0

    response_data = {
        "items": [PerfEvalResponse.model_validate(dict(item._mapping)).model_dump(mode="json") for item in items],
        "total": total,
        "page": page_num,
        "limit": limit_num
    }

    # ── 7. Backfill Dual-Tier Cache (L1 RAM + L2 Redis) ──────────────────
    if cache_key:
        is_immutable = bool(period and period < datetime.now().strftime("%Y-%m"))
        perf_cache.set(cache_key, response_data, is_immutable=is_immutable)

    return response_data



@router.get("/admin-kpi", response_model=dict)
def get_admin_perf_kpi(
    period: Optional[str] = None,
    project_id: Optional[str] = None,
    role_filter: Optional[str] = None,
    search: Optional[str] = None,
    type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "hr", "pm"))
):
    # ── 1. Dual-Tier Cache Check (L1 RAM -> L2 Redis) ───────────────────
    cache_key = None
    if period:
        cache_filters = {
            "project_id": project_id,
            "role_filter": role_filter,
            "search": search,
            "type": type,
            "scope": "admin_kpi",
        }
        cache_key = perf_cache.make_key(period, current_user.id, cache_filters, 1, 1)
        cached_result, _ = perf_cache.get(cache_key)
        if cached_result is not None:
            return cached_result

    # ── 2. Base Query Construction ──────────────────────────────────────
    q = db.query(PerfEvaluation)
    if period:
        q = q.filter(PerfEvaluation.period == period)
    if project_id and str(project_id) != "all":
        p_ids = [int(p.strip()) for p in str(project_id).split(",") if p.strip().isdigit()]
        if len(p_ids) == 1:
            q = q.filter(PerfEvaluation.project_id == p_ids[0])
        elif len(p_ids) > 1:
            q = q.filter(PerfEvaluation.project_id.in_(p_ids))
        
    has_emp_join = False
    if type in ["pm", "hr"] or search or role_filter:
        q = q.outerjoin(Employee, Employee.id == PerfEvaluation.employee_id)
        has_emp_join = True
        if type == "pm":
            q = q.filter(PerfEvaluation.project_id == 0, Employee.designation.ilike("%manager%"))
        elif type == "hr":
            q = q.filter(PerfEvaluation.project_id == 0, Employee.designation.ilike("%hr%"))
            
        if search and search.strip():
            q = q.filter(Employee.name.ilike(f"%{search.strip()}%"))
        if role_filter and str(role_filter) != "all":
            roles = [r.strip().lower() for r in str(role_filter).split(",") if r.strip()]
            role_conds = []
            for r in roles:
                if r == "admin":
                    role_conds.append(Employee.designation.ilike("%admin%"))
                elif r == "pm":
                    role_conds.append(Employee.designation.ilike("%manager%"))
                elif r == "team_lead":
                    role_conds.append(Employee.designation.ilike("%lead%"))
                elif r == "hr":
                    role_conds.append(Employee.designation.ilike("%hr%"))
                elif r == "annotator":
                    role_conds.append(Employee.designation.ilike("%annotator%"))
                elif r in ["full-time", "intern"]:
                    cond = Employee.employee_type.ilike(r)
                    if r == "full-time":
                        cond = and_(cond, ~Employee.designation.ilike("%manager%"), ~Employee.designation.ilike("%hr%"), ~Employee.designation.ilike("%lead%"))
                    role_conds.append(cond)
                elif r == "contract":
                    role_conds.append(Employee.employee_type.ilike("contract%"))
            if role_conds:
                q = q.filter(or_(*role_conds))
    
    # ── 3. Single Consolidated PostgreSQL Conditional Aggregation ───────
    # Collapses 5 separate COUNT / AVG queries into 1 SQL round-trip
    agg_row = q.with_entities(
        func.count(PerfEvaluation.id).label("total"),
        func.count(PerfEvaluation.id).filter(PerfEvaluation.status == "submitted").label("pending"),
        func.count(PerfEvaluation.id).filter(PerfEvaluation.status == "reviewed").label("reviewed"),
        func.count(PerfEvaluation.id).filter(PerfEvaluation.bonus_suggested == True).label("bonus"),
        func.avg(PerfEvaluation.overall_rating).filter(PerfEvaluation.overall_rating.isnot(None)).label("avg_val")
    ).one()

    total = agg_row.total or 0
    pending = agg_row.pending or 0
    reviewed = agg_row.reviewed or 0
    bonus = agg_row.bonus or 0
    avg_val = float(agg_row.avg_val) if agg_row.avg_val is not None else None

    # ── 4. Multi-Project and Multi-Bonus Employees (Group By + Single JOIN)
    multi_eval_names: List[str] = []
    multi_bonus_names: List[str] = []

    if total > 0:
        emp_join_q = q if has_emp_join else q.join(Employee, Employee.id == PerfEvaluation.employee_id)
        
        multi_eval_rows = emp_join_q.with_entities(
            Employee.name,
            func.count(PerfEvaluation.id)
        ).group_by(
            Employee.id, Employee.name
        ).having(
            func.count(PerfEvaluation.id) > 1
        ).all()
        multi_eval_names = [f"{name} ({cnt})" for name, cnt in multi_eval_rows]

        # Multi-bonus query only needed if bonus count > 1
        if bonus > 1:
            multi_bonus_rows = emp_join_q.filter(
                PerfEvaluation.bonus_suggested == True
            ).with_entities(
                Employee.name,
                func.count(PerfEvaluation.id)
            ).group_by(
                Employee.id, Employee.name
            ).having(
                func.count(PerfEvaluation.id) > 1
            ).all()
            multi_bonus_names = [f"{name} ({cnt})" for name, cnt in multi_bonus_rows]

    result = {
        "total": total,
        "multiEvalCount": len(multi_eval_names),
        "multiEvalNames": multi_eval_names,
        "pending": pending,
        "reviewed": reviewed,
        "completionRate": round((reviewed / total) * 100) if total > 0 else 0,
        "bonusCount": bonus,
        "multiBonusCount": len(multi_bonus_names),
        "multiBonusNames": multi_bonus_names,
        "avgRating": avg_val,
    }

    # ── 5. Backfill Dual-Tier Cache (L1 RAM + L2 Redis) ──────────────────
    if cache_key:
        is_immutable = bool(period and period < datetime.now().strftime("%Y-%m"))
        perf_cache.set(cache_key, result, is_immutable=is_immutable)

    return result

@router.get("/dashboard", response_model=dict)

def get_review_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm", "hr", "team_lead")),
):
    """Pre-scoped, pre-aggregated data for the Performance Reviews page.

    Replaces separately fetching parent-projects/sub-projects/employees/allocations/
    evaluations in full and filtering them client-side. Scoping matches
    getPmSubProjects + canDecideForEmployee exactly (see the helpers above) — this is a
    server-side port of the existing frontend logic, not a new policy.

    Gated the same as the mutating endpoints below (admin/pm/hr) rather than adding
    team_lead here: review_eval/delete_eval don't allow team_lead today, so granting
    them read access to an "approve" dashboard would be misleading.
    """
    pm_employee_id = current_user.employee_id
    role = current_user.role

    main_projects = db.query(MainProject).all()
    daily_sheets = db.query(DailySheet).all()
    allocations = (
        db.query(Allocation).filter(Allocation.employee_id == pm_employee_id).all()
        if pm_employee_id else []
    )

    scoped_projects = sorted(
        _get_pm_sub_projects(daily_sheets, main_projects, pm_employee_id, allocations),
        key=lambda p: (p.name or ""),
    )

    empty_summary = {"projects_in_scope": 0, "submissions_count": 0, "pending_count": 0}
    if not scoped_projects:
        return {"projects": [], "summary": empty_summary}

    scoped_ids = [p.id for p in scoped_projects]
    evals = (
        db.query(PerfEvaluation)
        .filter(PerfEvaluation.project_id.in_(scoped_ids))
        .order_by(PerfEvaluation.created_at.desc())
        .all()
    )

    employee_ids = {e.employee_id for e in evals}
    employees_by_id = (
        {e.id: e for e in db.query(Employee).filter(Employee.id.in_(employee_ids)).all()}
        if employee_ids else {}
    )

    reviewable_by_project: Dict[int, list] = {p.id: [] for p in scoped_projects}
    for ev in evals:
        employee = employees_by_id.get(ev.employee_id)
        if not _can_decide_for_employee(role, pm_employee_id, employee):
            continue
        reviewable_by_project.setdefault(ev.project_id, []).append(ev)

    projects_out = []
    submissions_count = 0
    pending_count = 0
    for project in scoped_projects:
        project_evals = reviewable_by_project.get(project.id, [])
        pending = sum(1 for e in project_evals if e.status == "submitted")
        submissions_count += len(project_evals)
        pending_count += pending
        projects_out.append({
            "id": project.id,
            "name": project.name,
            "client": getattr(project, "client", None),
            "submissions": len(project_evals),
            "pending": pending,
            "evaluations": [
                {
                    **PerfEvalResponse.model_validate(ev).model_dump(),
                    "employee_name": getattr(employees_by_id.get(ev.employee_id), "name", None),
                }
                for ev in project_evals
            ],
        })

    return {
        "projects": projects_out,
        "summary": {
            "projects_in_scope": len(scoped_projects),
            "submissions_count": submissions_count,
            "pending_count": pending_count,
        },
    }


@router.post("", response_model=PerfEvalResponse, status_code=201)
def create_eval(
    payload: PerfEvalCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    from zoneinfo import ZoneInfo
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    current_period = f"{now_ist.year:04d}-{now_ist.month:02d}"

    if current_user.role != "admin":
        # Standard lock: 20th to 24th of the current month
        if now_ist.day < 20 or now_ist.day > 24:
            raise HTTPException(
                status_code=403,
                detail="Performance evaluations can only be submitted between the 20th and 24th of the month.",
            )
        if payload.period != current_period:
            raise HTTPException(
                status_code=400,
                detail=f"Self-evaluations can only be submitted for the active month ({current_period}).",
            )

    # Deliberately NOT has_team_read: submitting an evaluation *for* someone else is a
    # manager's action. A team lead falls through to the self-check below, so it can still
    # file its own self-evaluation and nobody else's.
    if current_user.role not in ["admin", "pm", "hr"]:
        is_self = current_user.employee_id == payload.employee_id
        if not is_self:
            emp = db.query(Employee).filter(Employee.id == payload.employee_id).first()
            if not emp or emp.email != current_user.email:
                raise HTTPException(status_code=403, detail="Access denied")
    existing = (
        db.query(PerfEvaluation)
        .filter(
            PerfEvaluation.project_id == payload.project_id,
            PerfEvaluation.employee_id == payload.employee_id,
            PerfEvaluation.period == payload.period,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail="You have already submitted an evaluation for this project and month.",
        )

    param_values = [
        {"name": pv.name, "employee_rating": pv.employee_rating, "pm_rating": None, "approved": None, "feedback": None}
        for pv in payload.parameter_values
    ]

    ev = PerfEvaluation(
        project_id=payload.project_id,
        employee_id=payload.employee_id,
        period=payload.period,
        parameter_values=param_values,
        overall_comment=(payload.overall_comment or None),
        employee_overall_rating=_mean([pv.employee_rating for pv in payload.parameter_values]),
        status="submitted",
        # From the session, not the payload: this column records who filed the
        # self-evaluation and must not be settable by the client.
        submitted_by=current_user.id,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    perf_cache.invalidate_period(ev.period)
    try:
        _notify_on_submit(db, ev)
    except Exception:
        db.rollback()  # notifications are best-effort; never fail the submission
    return ev


@router.patch("/{eval_id}/review", response_model=PerfEvalResponse)
def review_eval(
    eval_id: int,
    payload: PerfEvalReview,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    ev = db.query(PerfEvaluation).filter(PerfEvaluation.id == eval_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluation not found")

    project_scope.require_employee_scope(
        db, current_user, ev.employee_id, action="review an evaluation"
    )

    # Merge the PM's review into the existing (employee-supplied) parameter rows.
    review_by_name = {p.name: p for p in payload.parameter_values}
    merged = []
    for row in (ev.parameter_values or []):
        name = row.get("name")
        r = review_by_name.get(name)
        if r is not None:
            merged.append({
                "name": name,
                "employee_rating": row.get("employee_rating"),
                "pm_rating": r.pm_rating,
                "approved": r.approved,
                "feedback": (r.feedback.strip() if (r.feedback and not r.approved) else None),
            })
        else:
            merged.append(row)

    ev.parameter_values = merged
    ev.overall_rating = _mean([p.pm_rating for p in payload.parameter_values])
    ev.bonus_suggested = bool(payload.bonus_suggested)
    ev.bonus_note = (payload.bonus_note.strip() if payload.bonus_note else None)
    ev.status = "reviewed"
    # Reviewer from the session — payload.reviewed_by is client-supplied and this is
    # the record of who signed off on someone's performance.
    ev.reviewed_by = current_user.id

    db.commit()
    db.refresh(ev)
    perf_cache.invalidate_period(ev.period)
    return ev


@router.delete("/{eval_id}")
def delete_eval(
    eval_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm")),
):
    ev = db.query(PerfEvaluation).filter(PerfEvaluation.id == eval_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluation not found")

    project_scope.require_employee_scope(
        db, current_user, ev.employee_id, action="delete an evaluation"
    )

    period_to_invalidate = ev.period
    db.delete(ev)
    db.commit()
    perf_cache.invalidate_period(period_to_invalidate)
    return {"message": "Evaluation deleted successfully"}