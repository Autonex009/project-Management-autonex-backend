"""
Encord analytics — served entirely from our own `encord_daily_time_spent` table
(never queries Encord live).

Metrics:
- platform_hours = sum(time_spent_seconds)/3600
- active annotator (a day) = user with an annotator role whose summed seconds that day > 3600
- avg_hours_per_annotator = platform_hours / count(distinct active annotators)
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.services.auth_service import require_role
from app.models.project import DailySheet
from app.models.encord_analytics import EncordDailyTimeSpent
from app.models.encord_activity import EncordDailyActivity
from app.models.employee import Employee

router = APIRouter(prefix="/api/analytics", tags=["Analytics"], dependencies=[Depends(require_role("admin", "pm"))])

ANNOTATOR_ROLES = {"ANNOTATOR", "ANNOTATOR_REVIEWER"}
REVIEWER_ROLES = {"REVIEWER", "ANNOTATOR_REVIEWER"}
ACTIVE_THRESHOLD_SECONDS = 3600

# Autonex employees use Encord accounts ending in this suffix.
AUTONEX_EMAIL_SUFFIX = "_theta@encord.ai"


def is_autonex_email(email: str | None) -> bool:
    return bool(email) and email.lower().endswith(AUTONEX_EMAIL_SUFFIX)


def _range_start(range_key: str | None, today: date) -> date:
    """Map a range key (1|7|30) to an inclusive start date."""
    days = {"1": 1, "7": 7, "30": 30}.get(str(range_key or "7"), 7)
    return today - timedelta(days=days - 1)


def _hours(seconds: int) -> float:
    return round((seconds or 0) / 3600.0, 2)


def _parse_date(s: Optional[str], default: date) -> date:
    if not s:
        return default
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date '{s}', expected YYYY-MM-DD")


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _names_for(db: Session, emails) -> dict:
    """Map Encord account emails -> employee display name via employees.encord_id.

    Falls back to the email itself for any Encord user not linked to an employee.
    """
    emails = {e for e in emails if e}
    if not emails:
        return {}
    rows = (
        db.query(Employee.encord_id, Employee.name)
        .filter(Employee.encord_id.in_(emails))
        .all()
    )
    return {encord_id: name for encord_id, name in rows if encord_id}


def _rows_for(db: Session, sp: DailySheet, start: date, end: date):
    """All time-spent rows for a project in [start, end] (inclusive)."""
    q = db.query(EncordDailyTimeSpent).filter(
        EncordDailyTimeSpent.metric_date >= start,
        EncordDailyTimeSpent.metric_date <= end,
    )
    if sp.encord_project_hash:
        q = q.filter(EncordDailyTimeSpent.encord_project_hash == sp.encord_project_hash)
    else:
        q = q.filter(EncordDailyTimeSpent.sub_project_id == sp.id)
    return q.all()


@router.get("/project/{sub_project_id}")
def project_analytics(
    sub_project_id: int,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    sp = db.query(DailySheet).filter(DailySheet.id == sub_project_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Project not found")

    today = date.today()
    start = _parse_date(date_from, _month_start(today))
    end = _parse_date(date_to, today)

    rows = _rows_for(db, sp, start, end)

    # (date, user) -> seconds ; (date, user) -> role
    du_seconds: dict = defaultdict(int)
    du_role: dict = {}
    date_seconds: dict = defaultdict(int)
    user_daily: dict = defaultdict(lambda: defaultdict(int))   # user -> date -> seconds
    user_role: dict = {}

    for r in rows:
        d, u = r.metric_date, r.user_email
        secs = r.time_spent_seconds or 0
        du_seconds[(d, u)] += secs
        date_seconds[d] += secs
        user_daily[u][d] += secs
        role = r.project_user_role
        if role:
            du_role[(d, u)] = role
            user_role.setdefault(u, role)

    def active_annotators_on(d: date) -> int:
        n = 0
        for (dd, u), secs in du_seconds.items():
            if dd == d and secs > ACTIVE_THRESHOLD_SECONDS and (du_role.get((dd, u)) in ANNOTATOR_ROLES):
                n += 1
        return n

    daily = []
    for d in sorted(date_seconds.keys()):
        active = active_annotators_on(d)
        hours = _hours(date_seconds[d])
        daily.append({
            "date": d.isoformat(),
            "platform_hours": hours,
            "active_annotators": active,
            "avg_hours_per_annotator": round(hours / active, 2) if active else 0.0,
        })

    # month/range consolidated
    total_seconds = sum(date_seconds.values())
    # distinct active annotators over the whole range (any day > threshold)
    range_active_users = {
        u for (d, u), secs in du_seconds.items()
        if secs > ACTIVE_THRESHOLD_SECONDS and du_role.get((d, u)) in ANNOTATOR_ROLES
    }
    month = {
        "platform_hours": _hours(total_seconds),
        "active_annotators_peak": max((x["active_annotators"] for x in daily), default=0),
        "active_annotators": len(range_active_users),
        "avg_hours_per_annotator": round(_hours(total_seconds) / len(range_active_users), 2) if range_active_users else 0.0,
    }

    name_by_email = _names_for(db, user_daily.keys())
    annotators = []
    for u, days in user_daily.items():
        total = sum(days.values())
        annotators.append({
            "user_email": u,
            "employee_name": name_by_email.get(u),   # real name, or None if unlinked (UI falls back to user_email)
            "role": user_role.get(u),
            "total_hours": _hours(total),
            "daily": [{"date": d.isoformat(), "hours": _hours(s)} for d, s in sorted(days.items())],
        })
    annotators.sort(key=lambda a: a["total_hours"], reverse=True)

    return {
        "project_id": sp.id,
        "name": sp.name,
        "client": sp.client,
        "encord_project_hash": sp.encord_project_hash,
        "sentiment": sp.sentiment,
        "range": {"from": start.isoformat(), "to": end.isoformat()},
        "daily": daily,
        "month": month,
        "annotators": annotators,
    }


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    today = date.today()
    start = _month_start(today)

    projects = (
        db.query(DailySheet)
        .filter(DailySheet.encord_project_hash.isnot(None))
        .filter(DailySheet.encord_project_hash != "")
        .all()
    )

    out = []
    for sp in projects:
        rows = _rows_for(db, sp, start, today)
        total_seconds = 0
        du_seconds: dict = defaultdict(int)
        du_role: dict = {}
        people = set()
        for r in rows:
            total_seconds += r.time_spent_seconds or 0
            people.add(r.user_email)
            du_seconds[(r.metric_date, r.user_email)] += r.time_spent_seconds or 0
            if r.project_user_role:
                du_role[(r.metric_date, r.user_email)] = r.project_user_role
        active_users = {
            u for (d, u), secs in du_seconds.items()
            if secs > ACTIVE_THRESHOLD_SECONDS and du_role.get((d, u)) in ANNOTATOR_ROLES
        }
        out.append({
            "project_id": sp.id,
            "name": sp.name,
            "client": sp.client,
            "status": sp.project_status,
            "encord_project_hash": sp.encord_project_hash,
            "month_platform_hours": _hours(total_seconds),
            "active_annotators": len(active_users),
            "people_involved": len(people),
            "sentiment": sp.sentiment,
        })
    out.sort(key=lambda p: p["month_platform_hours"], reverse=True)
    return out


# ── Autonex-only KPI helpers ─────────────────────────────────────────────────
def _autonex_kpis(db: Session, *, start: date, end: date, project_hash: str | None = None) -> dict:
    """Compute the 6 Autonex-only KPIs + a daily time series for a date range.

    If project_hash is given, scope to that Encord project; otherwise global.
    """
    time_q = db.query(EncordDailyTimeSpent).filter(
        EncordDailyTimeSpent.metric_date >= start,
        EncordDailyTimeSpent.metric_date <= end,
    )
    act_q = db.query(EncordDailyActivity).filter(
        EncordDailyActivity.metric_date >= start,
        EncordDailyActivity.metric_date <= end,
    )
    if project_hash:
        time_q = time_q.filter(EncordDailyTimeSpent.encord_project_hash == project_hash)
        act_q = act_q.filter(EncordDailyActivity.encord_project_hash == project_hash)

    total_seconds = 0
    annotation_seconds = 0
    review_seconds = 0
    daily_seconds: dict = defaultdict(int)
    for r in time_q.all():
        if not is_autonex_email(r.user_email):
            continue
        secs = r.time_spent_seconds or 0
        total_seconds += secs
        daily_seconds[r.metric_date] += secs
        role = (r.project_user_role or "").upper()
        if role in ANNOTATOR_ROLES:
            annotation_seconds += secs
        if role in REVIEWER_ROLES:
            review_seconds += secs

    tasks_submitted = 0
    labels_created = 0
    for a in act_q.all():
        if not is_autonex_email(a.user_email):
            continue
        tasks_submitted += a.tasks_submitted or 0
        labels_created += a.labels_created or 0

    avg_minutes_per_task = round((total_seconds / 60.0) / tasks_submitted, 1) if tasks_submitted else 0.0

    daily = [
        {"date": d.isoformat(), "hours": _hours(s)}
        for d, s in sorted(daily_seconds.items())
    ]

    return {
        "range": {"from": start.isoformat(), "to": end.isoformat()},
        "kpis": {
            "tasks_submitted": tasks_submitted,
            "total_hours": _hours(total_seconds),
            "annotation_hours": _hours(annotation_seconds),
            "labels_created": labels_created,
            "avg_minutes_per_task": avg_minutes_per_task,
            "review_hours": _hours(review_seconds),
        },
        "daily": daily,
    }


@router.get("/autonex/project/{sub_project_id}")
def autonex_project_kpis(
    sub_project_id: int,
    range: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """6 Autonex-only KPIs + daily time graph for one project, over the given range."""
    sp = db.query(DailySheet).filter(DailySheet.id == sub_project_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Project not found")
    today = date.today()
    start = _range_start(range, today)
    result = _autonex_kpis(db, start=start, end=today, project_hash=sp.encord_project_hash)
    result["project_id"] = sp.id
    result["name"] = sp.name
    return result


@router.get("/autonex/kpis")
def autonex_global_kpis(range: Optional[str] = None, db: Session = Depends(get_db)):
    """6 Autonex-only KPIs + daily time graph across ALL mapped projects, over the range."""
    today = date.today()
    start = _range_start(range, today)
    return _autonex_kpis(db, start=start, end=today, project_hash=None)


@router.get("/autonex/overview")
def autonex_overview(db: Session = Depends(get_db)):
    """Dashboard: most active Autonex user and most active project (by time), this month."""
    today = date.today()
    start = _month_start(today)

    rows = db.query(EncordDailyTimeSpent).filter(
        EncordDailyTimeSpent.metric_date >= start,
        EncordDailyTimeSpent.metric_date <= today,
    ).all()

    user_seconds: dict = defaultdict(int)
    project_seconds: dict = defaultdict(int)
    for r in rows:
        if not is_autonex_email(r.user_email):
            continue
        secs = r.time_spent_seconds or 0
        user_seconds[r.user_email] += secs
        project_seconds[r.encord_project_hash] += secs

    name_by_email = _names_for(db, user_seconds.keys())
    top_users = [
        {"user_email": u, "employee_name": name_by_email.get(u), "hours": _hours(s)}
        for u, s in sorted(user_seconds.items(), key=lambda kv: kv[1], reverse=True)
    ][:5]

    # Map project hash -> project name.
    hashes = [h for h in project_seconds.keys() if h]
    projects = db.query(DailySheet).filter(DailySheet.encord_project_hash.in_(hashes)).all() if hashes else []
    name_by_hash = {p.encord_project_hash: p.name for p in projects}
    id_by_hash = {p.encord_project_hash: p.id for p in projects}
    top_projects = [
        {"encord_project_hash": h, "project_id": id_by_hash.get(h), "name": name_by_hash.get(h, "Unmapped project"), "hours": _hours(s)}
        for h, s in sorted(project_seconds.items(), key=lambda kv: kv[1], reverse=True)
        if h
    ][:5]

    return {
        "range": {"from": start.isoformat(), "to": today.isoformat()},
        "autonex_total_hours": _hours(sum(user_seconds.values())),
        "top_users": top_users,
        "top_projects": top_projects,
    }
