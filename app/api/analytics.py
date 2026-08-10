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
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.services.auth_service import require_role, get_current_user
from app.models.project import DailySheet
from app.models.parent_project import MainProject
from app.models.sub_project import SubProject
from app.models.allocation import Allocation
from app.models.encord_analytics import EncordDailyTimeSpent
from app.models.encord_activity import EncordDailyActivity
from app.models.employee import Employee
from app.models.user import User

router = APIRouter(prefix="/api/analytics", tags=["Analytics"], dependencies=[Depends(require_role("admin", "pm"))])

# Self-service router: any authenticated user, but every endpoint here is strictly
# scoped to the caller's OWN Encord activity (resolved from their employee record).
# It carries no admin/pm dependency so employees can see their own dashboard chart.
me_router = APIRouter(prefix="/api/analytics", tags=["Analytics"])

ANNOTATOR_ROLES = {"ANNOTATOR", "ANNOTATOR_REVIEWER"}
REVIEWER_ROLES = {"REVIEWER", "ANNOTATOR_REVIEWER"}
ACTIVE_THRESHOLD_SECONDS = 3600

# Autonex employees use Encord accounts ending in this suffix.
AUTONEX_EMAIL_SUFFIX = "_theta@encord.ai"


def _get_pm_associated_sub_project_ids(db: Session, current_user: User) -> Optional[set[int]]:
    """Return set of sub-project (DailySheet) IDs accessible to the current user.

    Admins see everything (returns None).
    PMs (and HR) see projects where:
    - Their employee_id is in DailySheet.assigned_employee_ids
    - Or they are allocated to the sub-project in allocations
    - Or sub-project has no project-level PMs, but belongs to a parent MainProject
      where they are a program manager (program_manager_ids or program_manager_id).
    """
    if current_user.role == "admin":
        return None

    emp_id = current_user.employee_id
    if not emp_id and current_user.email:
        emp = db.query(Employee).filter(Employee.email == current_user.email).first()
        if emp:
            emp_id = emp.id

    if not emp_id:
        return set()

    # 1. Main projects managed by this PM
    all_parents = db.query(MainProject).all()
    managed_parent_ids = set()
    for mp in all_parents:
        pm_ids = set(mp.program_manager_ids or [])
        if mp.program_manager_id:
            pm_ids.add(mp.program_manager_id)
        if emp_id in pm_ids:
            managed_parent_ids.add(mp.id)

    # 2. Allocations for this PM
    allocated_sub_ids = {
        a.sub_project_id for a in db.query(Allocation.sub_project_id).filter(Allocation.employee_id == emp_id).all()
        if a.sub_project_id
    }

    # 3. SubProject (intermediate level) pm_id
    sub_projects = db.query(SubProject).filter(SubProject.pm_id == emp_id).all()
    sub_project_intermediate_ids = {sp.id for sp in sub_projects}

    # 4. Filter DailySheet rows
    daily_sheets = db.query(DailySheet).all()
    allowed_ids = set()
    for ds in daily_sheets:
        project_pms = list(ds.assigned_employee_ids or [])
        directly_assigned = emp_id in project_pms
        directly_allocated = ds.id in allocated_sub_ids
        intermediate_match = ds.sub_project_id in sub_project_intermediate_ids if ds.sub_project_id else False

        has_project_pm = len(project_pms) > 0
        org_fallback = (not has_project_pm) and bool(ds.main_project_id) and (ds.main_project_id in managed_parent_ids)

        if directly_assigned or directly_allocated or intermediate_match or org_fallback:
            allowed_ids.add(ds.id)

    return allowed_ids


def is_autonex_email(email: str | None) -> bool:
    # Stripped before the suffix test: a padded user_email would otherwise fail it
    # and drop the row out of the Autonex cohort entirely — not just mislabelled,
    # but missing from the leaderboard, the Autonex tab and the team averages.
    return bool(email) and email.strip().lower().endswith(AUTONEX_EMAIL_SUFFIX)


# Annotator / reviewer HEAD-COUNTS are classified by the Encord workflow stage the
# person actually worked in (not their permission role) — a user tagged TEAM_MANAGER
# who logs time in a "Review" stage is a reviewer for counting purposes. This is what
# makes the per-project counts reflect real work instead of showing 0 when everyone
# happens to hold a manager/admin role.
def _is_annotation_stage(stage: str | None) -> bool:
    return "annotate" in (stage or "").lower()


def _is_review_stage(stage: str | None) -> bool:
    return "review" in (stage or "").lower()


def _range_dates(
    range_key: str | None,
    today: date,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> tuple[date, date]:
    """Map a range key (1|7|30|custom) to an inclusive (start_date, end_date) pair."""
    key = str(range_key or "7")
    if key == "custom" and date_from:
        start = _parse_date(date_from, _month_start(today))
        end = _parse_date(date_to, today)
        return start, end
    if key == "1":
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday
    days = {"7": 7, "30": 30}.get(key, 7)
    return today - timedelta(days=days - 1), today


def _range_start(range_key: str | None, today: date) -> date:
    """Map a range key (1|7|30) to an inclusive start date."""
    start, _ = _range_dates(range_key, today)
    return start


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


# Whitespace an Encord id can pick up from a spreadsheet cell or a paste. Removed
# wholesale rather than trimmed at the edges: SQL ``trim`` strips spaces ONLY (in
# Postgres *and* SQLite), so a trailing \r\n from a copied cell survives it, and an
# id is an email address — it can never legitimately contain any of these anyway.
_ENCORD_WHITESPACE = (" ", "\t", "\n", "\r", "\v", "\f", " ")


def _norm_encord(value: Optional[str]) -> str:
    """Canonical form of an Encord account id, for comparing ours against theirs.

    A chunk of `employees.encord_id` was imported from a spreadsheet and carries
    stray whitespace (see the ``trim`` in api\\employees.py's search). An exact
    `encord_id == user_email` match silently misses those rows, and every caller
    here degrades the same way — it falls back to printing the raw Encord email,
    so a correctly-linked employee looks unlinked in the charts.
    """
    normalized = value or ""
    for ch in _ENCORD_WHITESPACE:
        normalized = normalized.replace(ch, "")
    return normalized.lower()


def _sql_norm_encord(column):
    """`_norm_encord` expressed in SQL, so the two sides agree exactly.

    Nested ``replace`` + ``lower``: the only whitespace-stripping shape portable to
    both Postgres and the SQLite fallback engine (regexp_replace exists in neither
    pair, and two-arg trim has no common signature). The characters ride along as
    bound parameters, so no dialect-specific char()/chr() either.
    """
    expr = column
    for ch in _ENCORD_WHITESPACE:
        expr = func.replace(expr, ch, "")
    return func.lower(expr)


def _encord_id_matches(emails):
    """SQL predicate: employees.encord_id equals any of `emails`, ignoring whitespace and case."""
    return _sql_norm_encord(Employee.encord_id).in_(
        sorted({_norm_encord(e) for e in emails if e})
    )


def _names_for(db: Session, emails) -> dict:
    """Map Encord account emails -> employee display name via employees.encord_id.

    Keyed by the email as the caller passed it (i.e. as stored in
    `encord_daily_time_spent`), so callers can look up with their own value.
    Falls back to the email itself for any Encord user not linked to an employee.
    """
    emails = {e for e in emails if e}
    if not emails:
        return {}
    rows = (
        db.query(Employee.encord_id, Employee.name)
        .filter(_encord_id_matches(emails))
        .all()
    )
    name_by_norm = {_norm_encord(enc): name for enc, name in rows if enc and name}
    return {
        email: name_by_norm[_norm_encord(email)]
        for email in emails
        if _norm_encord(email) in name_by_norm
    }


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
    current_user: User = Depends(get_current_user),
):
    allowed_ids = _get_pm_associated_sub_project_ids(db, current_user)
    if allowed_ids is not None and sub_project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="Access denied to this project's analytics")

    sp = db.query(DailySheet).filter(DailySheet.id == sub_project_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Project not found")

    today = date.today()
    start = _parse_date(date_from, _month_start(today))
    end = _parse_date(date_to, today)

    rows = _rows_for(db, sp, start, end)

    # Autonex team only (billing view). An "active annotator" on a day = an Autonex
    # user with >1h of annotation-stage work that day (same definition as the summary
    # table / KPIs), so counts stay consistent across the dashboard.
    du_ann_seconds: dict = defaultdict(int)   # (date, user) -> annotation-stage secs
    date_seconds: dict = defaultdict(int)      # date -> Autonex platform secs
    user_daily: dict = defaultdict(lambda: defaultdict(int))   # user -> date -> seconds
    user_role: dict = {}

    for r in rows:
        if not is_autonex_email(r.user_email):
            continue
        d, u = r.metric_date, r.user_email
        secs = r.time_spent_seconds or 0
        date_seconds[d] += secs
        user_daily[u][d] += secs
        if _is_annotation_stage(r.workflow_stage):
            du_ann_seconds[(d, u)] += secs
        if r.project_user_role:
            user_role.setdefault(u, r.project_user_role)

    def active_annotators_on(d: date) -> int:
        return sum(
            1 for (dd, u), secs in du_ann_seconds.items()
            if dd == d and secs > ACTIVE_THRESHOLD_SECONDS
        )

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
        u for (d, u), secs in du_ann_seconds.items()
        if secs > ACTIVE_THRESHOLD_SECONDS
    }
    people_count = len(user_daily)   # distinct Autonex people who logged any time
    total_hours = _hours(total_seconds)
    month = {
        "platform_hours": total_hours,
        "active_annotators_peak": max((x["active_annotators"] for x in daily), default=0),
        "active_annotators": len(range_active_users),
        # avg per active annotator (>1h annotation/day)
        "avg_hours_per_annotator": round(total_hours / len(range_active_users), 2) if range_active_users else 0.0,
        # avg across everyone who touched the project (matches the People count)
        "people": people_count,
        "avg_hours_per_person": round(total_hours / people_count, 2) if people_count else 0.0,
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

    # Fixed reference windows (independent of the selected range), all ending today:
    # today (1d), last 7 days, last 30 days. Autonex-only, stage-based annotators.
    # One 30-day query, derived in Python for the shorter windows.
    win_rows = [
        (r.metric_date, r.user_email, r.time_spent_seconds or 0, r.workflow_stage)
        for r in _rows_for(db, sp, today - timedelta(days=29), today)
        if is_autonex_email(r.user_email)
    ]

    def _summarise(entries) -> dict:
        total = 0
        ann_day: dict = defaultdict(int)
        for d, u, secs, stage in entries:
            total += secs
            if _is_annotation_stage(stage):
                ann_day[(d, u)] += secs
        active = {u for (d, u), s in ann_day.items() if s > ACTIVE_THRESHOLD_SECONDS}
        n = len(active)
        hrs = _hours(total)
        return {
            "active_annotators": n,
            "platform_hours": hrs,
            "avg_hours_per_annotator": round(hrs / n, 2) if n else 0.0,
        }

    # "Today" = the latest day that actually has data. The sync runs ~23:30, so the
    # current day is usually empty until then — fall back to the most recent synced
    # day (typically yesterday) and report which date it is. Rolling 7/30-day figures
    # aren't computed here: the range toggle already drives the range-based cards.
    latest_date = max((e[0] for e in win_rows), default=None)
    day = _summarise([e for e in win_rows if e[0] == latest_date]) if latest_date else _summarise([])
    day["date"] = latest_date.isoformat() if latest_date else None

    fixed = {"today": day}

    # Project PM name(s): the project's own assigned PMs, falling back to the
    # organization's PMs when the project has none.
    pm_ids = list(sp.assigned_employee_ids or [])
    if not pm_ids and sp.main_project_id:
        org = db.query(MainProject).filter(MainProject.id == sp.main_project_id).first()
        if org:
            pm_ids = org.program_manager_ids or ([org.program_manager_id] if org.program_manager_id else [])
    pm_names = []
    if pm_ids:
        name_by_id = dict(
            db.query(Employee.id, Employee.name).filter(Employee.id.in_(pm_ids)).all()
        )
        pm_names = [name_by_id[i] for i in pm_ids if i in name_by_id]

    return {
        "project_id": sp.id,
        "name": sp.name,
        "client": sp.client,
        "pm_names": pm_names,
        "encord_project_hash": sp.encord_project_hash,
        "sentiment": sp.sentiment,
        "range": {"from": start.isoformat(), "to": end.isoformat()},
        "daily": daily,
        "month": month,
        "annotators": annotators,
        "fixed": fixed,
    }


@router.get("/summary")
def summary(
    range: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    today = date.today()
    # Window follows the dashboard range toggle (1|7|30 days|custom). Falls back to
    # month-to-date when no range is given, preserving the old default.
    start, end = _range_dates(range, today, date_from, date_to) if range else (_month_start(today), today)

    allowed_ids = _get_pm_associated_sub_project_ids(db, current_user)
    query = (
        db.query(DailySheet)
        .filter(DailySheet.encord_project_hash.isnot(None))
        .filter(DailySheet.encord_project_hash != "")
    )
    if allowed_ids is not None:
        query = query.filter(DailySheet.id.in_(allowed_ids))
    projects = query.all()

    out = []
    for sp in projects:
        rows = _rows_for(db, sp, start, end)
        total_seconds = 0
        du_seconds: dict = defaultdict(int)
        du_role: dict = {}
        people = set()
        # Autonex-only (billing) accumulators. Active annotator/reviewer is decided
        # by the workflow STAGE worked in: a (day, user) counts toward annotators if
        # its annotation-stage seconds that day exceed the threshold, toward reviewers
        # likewise for review stages. Someone who both annotates and reviews counts in
        # both (two independent head-counts, never summed).
        ax_total = 0
        ax_people = set()
        ax_ann_day: dict = defaultdict(int)   # (date, user) -> annotator-role secs
        ax_rev_day: dict = defaultdict(int)   # (date, user) -> reviewer-role secs
        for r in rows:
            secs = r.time_spent_seconds or 0
            total_seconds += secs
            people.add(r.user_email)
            du_seconds[(r.metric_date, r.user_email)] += secs
            if r.project_user_role:
                du_role[(r.metric_date, r.user_email)] = r.project_user_role
            if is_autonex_email(r.user_email):
                ax_total += secs
                ax_people.add(r.user_email)
                key = (r.metric_date, r.user_email)
                if _is_annotation_stage(r.workflow_stage):
                    ax_ann_day[key] += secs
                if _is_review_stage(r.workflow_stage):
                    ax_rev_day[key] += secs
        active_users = {
            u for (d, u), secs in du_seconds.items()
            if secs > ACTIVE_THRESHOLD_SECONDS and du_role.get((d, u)) in ANNOTATOR_ROLES
        }
        ax_annotators = {u for (d, u), s in ax_ann_day.items() if s > ACTIVE_THRESHOLD_SECONDS}
        ax_reviewers = {u for (d, u), s in ax_rev_day.items() if s > ACTIVE_THRESHOLD_SECONDS}
        # Mutually exclusive buckets so the columns sum cleanly: a person is either
        # annotation-only, review-only, or both. (Sum = everyone who did >1h of stage
        # work; ax_people may be larger — it also counts sub-threshold / other stages.)
        ax_both = ax_annotators & ax_reviewers
        ax_ann_only = ax_annotators - ax_reviewers
        ax_rev_only = ax_reviewers - ax_annotators
        # Resolve Encord emails -> employee display names (falls back to the email).
        names_map = _names_for(db, ax_annotators | ax_reviewers)
        disp = lambda emails: sorted(names_map.get(e, e) for e in emails)
        out.append({
            "project_id": sp.id,
            "name": sp.name,
            "client": sp.client,
            "status": sp.project_status,
            "encord_project_hash": sp.encord_project_hash,
            "month_platform_hours": _hours(total_seconds),
            "active_annotators": len(active_users),
            "people_involved": len(people),
            # Autonex-only billing figures (team members: *_theta@encord.ai)
            "autonex_platform_hours": _hours(ax_total),
            "autonex_active_annotators": len(ax_annotators),
            "autonex_active_reviewers": len(ax_reviewers),
            # Mutually exclusive head-count buckets (+ names for hover)
            "autonex_annotator_only": len(ax_ann_only),
            "autonex_reviewer_only": len(ax_rev_only),
            "autonex_both": len(ax_both),
            "autonex_annotator_only_names": disp(ax_ann_only),
            "autonex_reviewer_only_names": disp(ax_rev_only),
            "autonex_both_names": disp(ax_both),
            "autonex_people": len(ax_people),
            "sentiment": sp.sentiment,
        })
    out.sort(key=lambda p: p["autonex_platform_hours"], reverse=True)
    return out


# ── Autonex-only KPI helpers ─────────────────────────────────────────────────
def _autonex_kpis(db: Session, *, start: date, end: date, project_hash: str | None = None, allowed_hashes: set[str] | None = None) -> dict:
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
    elif allowed_hashes is not None:
        time_q = time_q.filter(EncordDailyTimeSpent.encord_project_hash.in_(allowed_hashes))
        act_q = act_q.filter(EncordDailyActivity.encord_project_hash.in_(allowed_hashes))

    total_seconds = 0
    annotation_seconds = 0
    review_seconds = 0
    other_seconds = 0
    other_stages: dict = defaultdict(int)   # stage name -> secs (neither annotate nor review)
    daily_seconds: dict = defaultdict(int)
    people = set()
    ann_day: dict = defaultdict(int)   # (date, user) -> annotator-role secs that day
    rev_day: dict = defaultdict(int)   # (date, user) -> reviewer-role secs that day
    for r in time_q.all():
        if not is_autonex_email(r.user_email):
            continue
        secs = r.time_spent_seconds or 0
        total_seconds += secs
        daily_seconds[r.metric_date] += secs
        people.add(r.user_email)
        key = (r.metric_date, r.user_email)
        # Hours AND head-counts both keyed by workflow stage: a row's seconds only
        # count toward annotation_seconds if the row's own stage is an annotate stage,
        # and only toward review_seconds if the row's own stage is a review stage.
        # Anything else (Skipped Tasks, Agent Rejected, Consensus, ...) is "Other", so
        # annotation + review + other == platform hours exactly.
        is_ann = _is_annotation_stage(r.workflow_stage)
        is_rev = _is_review_stage(r.workflow_stage)
        if is_ann:
            annotation_seconds += secs
            ann_day[key] += secs
        if is_rev:
            review_seconds += secs
            rev_day[key] += secs
        if not is_ann and not is_rev:
            other_seconds += secs
            other_stages[r.workflow_stage or "(none)"] += secs

    # Distinct people, active = >1h of annotation- (or review-) stage work on a day.
    active_annotators = len({u for (d, u), s in ann_day.items() if s > ACTIVE_THRESHOLD_SECONDS})
    active_reviewers = len({u for (d, u), s in rev_day.items() if s > ACTIVE_THRESHOLD_SECONDS})

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
            "other_hours": _hours(other_seconds),
            # per-stage breakdown of the "Other" bucket, for the hover tooltip
            "other_breakdown": [
                {"stage": s, "hours": _hours(sec)}
                for s, sec in sorted(other_stages.items(), key=lambda kv: kv[1], reverse=True)
            ],
            "active_annotators": active_annotators,
            "active_reviewers": active_reviewers,
            "people": len(people),
        },
        "daily": daily,
    }


@router.get("/autonex/project/{sub_project_id}")
def autonex_project_kpis(
    sub_project_id: int,
    range: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """6 Autonex-only KPIs + daily time graph for one project, over the given range."""
    allowed_ids = _get_pm_associated_sub_project_ids(db, current_user)
    if allowed_ids is not None and sub_project_id not in allowed_ids:
        raise HTTPException(status_code=403, detail="Access denied to this project's analytics")

    sp = db.query(DailySheet).filter(DailySheet.id == sub_project_id).first()
    if not sp:
        raise HTTPException(status_code=404, detail="Project not found")
    today = date.today()
    start, end = _range_dates(range, today, date_from, date_to) if range else (_month_start(today), today)
    result = _autonex_kpis(db, start=start, end=end, project_hash=sp.encord_project_hash)
    result["project_id"] = sp.id
    result["name"] = sp.name
    return result


@router.get("/autonex/kpis")
def autonex_global_kpis(
    range: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """6 Autonex-only KPIs + daily time graph across ALL mapped projects, over the range."""
    today = date.today()
    start, end = _range_dates(range, today, date_from, date_to) if range else (_month_start(today), today)
    allowed_ids = _get_pm_associated_sub_project_ids(db, current_user)
    allowed_hashes = None
    if allowed_ids is not None:
        hashes = [
            h[0] for h in db.query(DailySheet.encord_project_hash).filter(DailySheet.id.in_(allowed_ids)).all() if h[0]
        ]
        allowed_hashes = set(hashes)
    return _autonex_kpis(db, start=start, end=end, project_hash=None, allowed_hashes=allowed_hashes)


@router.get("/autonex/overview")
def autonex_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dashboard: most active Autonex user and most active project (by time), this month."""
    today = date.today()
    start = _month_start(today)

    allowed_ids = _get_pm_associated_sub_project_ids(db, current_user)
    allowed_hashes = None
    if allowed_ids is not None:
        hashes = [
            h[0] for h in db.query(DailySheet.encord_project_hash).filter(DailySheet.id.in_(allowed_ids)).all() if h[0]
        ]
        allowed_hashes = set(hashes)

    rows = db.query(EncordDailyTimeSpent).filter(
        EncordDailyTimeSpent.metric_date >= start,
        EncordDailyTimeSpent.metric_date <= today,
    ).all()

    user_seconds: dict = defaultdict(int)
    project_seconds: dict = defaultdict(int)
    ann_day: dict = defaultdict(int)   # (date, user) -> annotator-role secs that day
    rev_day: dict = defaultdict(int)   # (date, user) -> reviewer-role secs that day
    for r in rows:
        if not is_autonex_email(r.user_email):
            continue
        if allowed_hashes is not None and r.encord_project_hash not in allowed_hashes:
            continue
        secs = r.time_spent_seconds or 0
        user_seconds[r.user_email] += secs
        project_seconds[r.encord_project_hash] += secs
        key = (r.metric_date, r.user_email)
        if _is_annotation_stage(r.workflow_stage):
            ann_day[key] += secs
        if _is_review_stage(r.workflow_stage):
            rev_day[key] += secs

    # Distinct people, deduped across every project. Active = >1h of annotation- (or
    # review-) stage work on at least one day. Someone who does both counts in both.
    active_annotators = {u for (d, u), s in ann_day.items() if s > ACTIVE_THRESHOLD_SECONDS}
    active_reviewers = {u for (d, u), s in rev_day.items() if s > ACTIVE_THRESHOLD_SECONDS}

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
        "active_annotators": len(active_annotators),
        "active_reviewers": len(active_reviewers),
        "autonex_people": len(user_seconds),
        "top_users": top_users,
        "top_projects": top_projects,
    }


def _resolve_employee(db: Session, current_user: User) -> Employee | None:
    """Find the Employee record for the signed-in user (by employee_id, else email)."""
    emp = None
    if current_user.employee_id:
        emp = db.query(Employee).filter(Employee.id == current_user.employee_id).first()
    if not emp and current_user.email:
        emp = db.query(Employee).filter(Employee.email == current_user.email).first()
    return emp


@me_router.get("/me/encord-activity")
def my_encord_activity(
    days: int = 7,
    sub_project_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The signed-in user's OWN Encord platform hours per day (last `days` days).

    Resolves the caller's Encord account via employees.encord_id, then reads their
    daily time from `encord_daily_time_spent`. Optionally scoped to one sub-project
    (the employee's current project) — which also yields a per-day team average so
    the dashboard can compare the individual against their project's teammates.

    Returns `{ total_hours, daily: [{date, employee_hours, team_avg_hours}], ... }`.
    An unmapped employee (no encord_id) returns an empty, `mapped: false` payload
    rather than an error, so the dashboard renders cleanly.
    """
    days = max(1, min(int(days or 7), 90))

    emp = _resolve_employee(db, current_user)
    encord_id = (emp.encord_id or "").strip() if emp else ""

    # Window: the last `days` days ending on the latest synced date (the sync runs
    # once a day, ~11:30pm, so "today" may not be in yet — anchoring to the latest
    # available date guarantees the chart shows the most recent complete data).
    latest = db.query(func.max(EncordDailyTimeSpent.metric_date)).scalar()
    end = latest or date.today()
    start = end - timedelta(days=days - 1)

    empty = {
        "mapped": bool(encord_id),
        "encord_id": encord_id or None,
        "range": {"from": start.isoformat(), "to": end.isoformat(), "days": days},
        "total_hours": 0.0,
        "daily": [],
    }
    if not encord_id:
        return empty

    # Optional project scoping. Prefer the Encord hash (rows are keyed by hash);
    # fall back to the sub_project_id.
    sp = db.query(DailySheet).filter(DailySheet.id == sub_project_id).first() if sub_project_id else None
    project_hash = sp.encord_project_hash if sp else None

    base = db.query(EncordDailyTimeSpent).filter(
        EncordDailyTimeSpent.metric_date >= start,
        EncordDailyTimeSpent.metric_date <= end,
    )
    if sp is not None:
        base = base.filter(
            EncordDailyTimeSpent.encord_project_hash == project_hash
        ) if project_hash else base.filter(EncordDailyTimeSpent.sub_project_id == sp.id)

    rows = base.all()

    # Per-day: this employee's seconds, and per-day team totals for the average.
    mine_by_day: dict[date, int] = defaultdict(int)
    team_secs_by_day: dict[date, int] = defaultdict(int)
    team_users_by_day: dict[date, set] = defaultdict(set)
    for r in rows:
        d = r.metric_date
        secs = int(r.time_spent_seconds or 0)
        if _norm_encord(r.user_email) == _norm_encord(encord_id):
            mine_by_day[d] += secs
        # Team average is over Autonex teammates (billing cohort), including self.
        if is_autonex_email(r.user_email):
            team_secs_by_day[d] += secs
            team_users_by_day[d].add(r.user_email)

    daily = []
    for i in range(days):
        d = start + timedelta(days=i)
        team_n = len(team_users_by_day.get(d, ()))
        team_avg = _hours(team_secs_by_day.get(d, 0) / team_n) if team_n else 0.0
        daily.append({
            "date": d.isoformat(),
            "employee_hours": _hours(mine_by_day.get(d, 0)),
            "team_avg_hours": team_avg,
        })

    return {
        "mapped": True,
        "encord_id": encord_id,
        "range": {"from": start.isoformat(), "to": end.isoformat(), "days": days},
        "total_hours": _hours(sum(mine_by_day.values())),
        "daily": daily,
    }


@me_router.get("/leaderboard")
def get_leaderboard(
    range: Optional[str] = "month",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Admin leaderboard: rank Autonex team members by platform hours over preset windows or custom dates."""
    today = date.today()
    if range == "custom" and date_from:
        start = _parse_date(date_from, _month_start(today))
        end = _parse_date(date_to, today)
    elif range == "day":
        yesterday = today - timedelta(days=1)
        start = yesterday
        end = yesterday
    elif range == "week":
        start = today - timedelta(days=6)
        end = today
    elif range in ("year", "overall"):
        start = date(today.year, 1, 1)
        end = today
    else:  # "month" or default
        start = _month_start(today)
        end = today

    time_rows = db.query(EncordDailyTimeSpent).filter(
        EncordDailyTimeSpent.metric_date >= start,
        EncordDailyTimeSpent.metric_date <= end,
    ).all()

    act_rows = db.query(EncordDailyActivity).filter(
        EncordDailyActivity.metric_date >= start,
        EncordDailyActivity.metric_date <= end,
    ).all()

    user_seconds: dict = defaultdict(int)
    annotation_seconds: dict = defaultdict(int)
    review_seconds: dict = defaultdict(int)
    tasks_submitted_by_user: dict = defaultdict(int)
    labels_created_by_user: dict = defaultdict(int)

    user_project_seconds = defaultdict(lambda: defaultdict(int))

    for r in time_rows:
        if not is_autonex_email(r.user_email):
            continue
        secs = r.time_spent_seconds or 0
        u = r.user_email
        user_seconds[u] += secs
        if _is_annotation_stage(r.workflow_stage):
            annotation_seconds[u] += secs
        if _is_review_stage(r.workflow_stage):
            review_seconds[u] += secs

        if r.sub_project_id:
            user_project_seconds[u][("id", r.sub_project_id)] += secs
        elif r.encord_project_hash:
            user_project_seconds[u][("hash", r.encord_project_hash)] += secs

    for a in act_rows:
        if not is_autonex_email(a.user_email):
            continue
        u = a.user_email
        tasks_submitted_by_user[u] += a.tasks_submitted or 0
        labels_created_by_user[u] += a.labels_created or 0

    total_team_seconds = sum(user_seconds.values())
    name_by_email = _names_for(db, user_seconds.keys())

    # Map project IDs and hashes to project names from DailySheet
    all_p_ids = {pid for pmap in user_project_seconds.values() for ptype, pid in pmap.keys() if ptype == "id"}
    all_p_hashes = {phash for pmap in user_project_seconds.values() for ptype, phash in pmap.keys() if ptype == "hash"}

    proj_name_by_id = {}
    proj_name_by_hash = {}
    if all_p_ids:
        for ds in db.query(DailySheet.id, DailySheet.name).filter(DailySheet.id.in_(all_p_ids)).all():
            proj_name_by_id[ds.id] = ds.name
    if all_p_hashes:
        for ds in db.query(DailySheet.encord_project_hash, DailySheet.name).filter(DailySheet.encord_project_hash.in_(all_p_hashes)).all():
            if ds.encord_project_hash and ds.name:
                proj_name_by_hash[ds.encord_project_hash] = ds.name

    emails = [e for e in user_seconds.keys() if e]
    # Normalized on both sides, like _names_for — otherwise a whitespace-padded
    # encord_id costs the row its avatar and designation as well as its name.
    emp_rows = db.query(Employee.encord_id, Employee.email, Employee.designation, Employee.id, Employee.avatar_url).filter(
        _encord_id_matches(emails)
        | _sql_norm_encord(Employee.email).in_(sorted({_norm_encord(e) for e in emails}))
    ).all() if emails else []

    emp_map = {}
    for enc_id, email, desig, emp_id, avatar in emp_rows:
        info = (desig, emp_id, avatar)
        if enc_id:
            emp_map[_norm_encord(enc_id)] = info
        if email:
            emp_map.setdefault(_norm_encord(email), info)

    leaderboard = []
    sorted_users = sorted(user_seconds.items(), key=lambda kv: kv[1], reverse=True)
    for rank, (u, secs) in enumerate(sorted_users, 1):
        hrs = _hours(secs)
        ann_hrs = _hours(annotation_seconds[u])
        rev_hrs = _hours(review_seconds[u])
        desig, emp_id, avatar_url = emp_map.get(_norm_encord(u), (None, None, None))
        pct = round((secs / total_team_seconds) * 100, 1) if total_team_seconds else 0.0

        # Determine user's primary project name
        top_proj_name = None
        if u in user_project_seconds and user_project_seconds[u]:
            sorted_projs = sorted(user_project_seconds[u].items(), key=lambda kv: kv[1], reverse=True)
            for (ptype, pval), _ in sorted_projs:
                pname = proj_name_by_id.get(pval) if ptype == "id" else proj_name_by_hash.get(pval)
                if pname:
                    top_proj_name = pname
                    break

        if not top_proj_name and emp_id:
            # Fallback check if user assigned in daily sheets
            assigned = db.query(DailySheet.name).filter(DailySheet.assigned_employee_ids.contains([emp_id])).first()
            if assigned:
                top_proj_name = assigned.name

        leaderboard.append({
            "rank": rank,
            "user_email": u,
            "employee_name": name_by_email.get(u),
            "employee_id": emp_id,
            "avatar_url": avatar_url,
            "designation": desig or "Annotator / Reviewer",
            "project_name": top_proj_name or "General",
            "total_hours": hrs,
            "annotation_hours": ann_hrs,
            "review_hours": rev_hrs,
            "tasks_submitted": tasks_submitted_by_user[u],
            "labels_created": labels_created_by_user[u],
            "share_percentage": pct,
        })

    top_performer = leaderboard[0] if leaderboard else None

    return {
        "range": {"from": start.isoformat(), "to": end.isoformat()},
        "team_summary": {
            "total_hours": _hours(total_team_seconds),
            "active_users": len(user_seconds),
            "total_tasks": sum(tasks_submitted_by_user.values()),
            "top_performer": {
                "name": (top_performer["employee_name"] or top_performer["user_email"]) if top_performer else "—",
                "hours": top_performer["total_hours"] if top_performer else 0.0,
            } if top_performer else None,
        },
        "leaderboard": leaderboard,
    }
