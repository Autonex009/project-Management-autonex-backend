"""Read-only API over the audit log.

There are deliberately no POST / PUT / PATCH / DELETE routes here. Entries are
written only by ``app.services.audit_service.record`` from inside the endpoint that
performed the action, and are never modified afterwards. An audit trail the
application can edit is not an audit trail.
"""
from datetime import date as date_type, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.audit_log import AuditLog
from app.models.employee import Employee
from app.models.user import User
from app.services.audit_service import RETENTION_DAYS, prune_expired_logs
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/api/audit-logs", tags=["Audit Logs"])

# Actions that walk back an earlier decision. Surfaced as their own stat and
# highlighted in the UI, because "who reversed this?" is the question an audit log
# exists to answer. Keep in sync with REVERSAL_ACTIONS in ChangeLogPage.jsx.
REVERSAL_ACTIONS = [
    "leave.approval_revoked",
    "leave.reject_undone",
    "wfh.approval_revoked",
    "wfh.reject_undone",
    "signup_request.approval_revoked",
    "signup_request.reject_undone",
    "employee.restored",
]

# A page bigger than this is never served, whatever the client asks for — an
# unbounded page_size is a trivial way to exhaust memory.
MAX_PAGE_SIZE = 100
# Deep offsets get progressively more expensive; past this, filters are the answer.
MAX_PAGE = 1000


def require_admin_only(user: User = Depends(get_current_user)) -> User:
    """Strict admin gate, used only by this router.

    Deliberately *not* ``require_role("admin")``: that shared helper also admits HR,
    because HR carries combined admin + PM access everywhere else in the app. The
    audit log spans payroll, salary and employee actions for the whole organisation,
    so it is limited to ``role == "admin"`` exactly.

    To let HR in as well, swap the dependency for ``require_role("admin")`` — do not
    change ``require_role`` itself, which the rest of the app relies on.
    """
    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Access denied. The audit log is restricted to admins.",
        )
    return user


def _serialize(entry: AuditLog, avatar_url: Optional[str]) -> dict:
    """Shape one row for the frontend.

    ``actor_*`` values are read straight off the audit row rather than joined from
    ``users`` — they are point-in-time snapshots, so a later rename or role change
    must not rewrite history. ``avatar_url`` is the exception: a profile photo is
    cosmetic, so the current one is resolved at read time.
    """
    return {
        "id": entry.id,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "action": entry.action,
        "action_type": entry.action_type,
        "category": entry.category,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "entity_name": entry.entity_name,
        "subject_name": entry.subject_name,
        "subject_employee_id": entry.subject_employee_id,
        "summary": entry.summary,
        "details": entry.details or [],
        "ip": entry.ip,
        "actor": {
            "id": entry.actor_id,
            "name": entry.actor_name or "System",
            "email": entry.actor_email,
            "role": entry.actor_role or "system",
            "avatar_url": avatar_url,
        },
    }


def _avatars_for(db: Session, entries: list[AuditLog]) -> dict:
    """Look up current avatars for the actors on this page.

    One small extra query keyed on primary keys, rather than joining ``users`` and
    ``employees`` into the main listing query — that keeps the paginated query
    single-table so it can use the ``created_at`` index without a join getting in
    the way.
    """
    actor_ids = {e.actor_id for e in entries if e.actor_id}
    if not actor_ids:
        return {}
    rows = (
        db.query(User.id, Employee.avatar_url)
        .outerjoin(Employee, Employee.id == User.employee_id)
        .filter(User.id.in_(actor_ids))
        .all()
    )
    return {user_id: avatar for user_id, avatar in rows}


@router.get("")
def list_audit_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1),
    search: Optional[str] = None,
    category: Optional[str] = None,
    action_type: Optional[str] = None,
    actor_id: Optional[int] = None,
    actor_role: Optional[str] = None,
    subject_employee_id: Optional[int] = None,
    entity_type: Optional[str] = None,
    date_from: Optional[date_type] = None,
    date_to: Optional[date_type] = None,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin_only),
):
    """Paginated, filtered listing — newest first.

    Server-side pagination: the client receives one page plus ``total``, and never
    the whole table.
    """
    if page > MAX_PAGE:
        raise HTTPException(
            status_code=400,
            detail=f"Page {page} is too deep. Narrow the filters or date range instead.",
        )
    page_size = min(page_size, MAX_PAGE_SIZE)

    # Automatically prune entries older than 20 days
    prune_expired_logs(db)

    # 20-day retention cutoff
    retention_cutoff = date_type.today() - timedelta(days=RETENTION_DAYS)

    query = db.query(AuditLog).filter(AuditLog.created_at >= retention_cutoff)

    if category and category.lower() != "all":
        query = query.filter(AuditLog.category == category)
    if action_type and action_type.lower() != "all":
        query = query.filter(AuditLog.action_type == action_type)
    if actor_id:
        query = query.filter(AuditLog.actor_id == actor_id)
    if actor_role and actor_role.lower() != "all":
        # Lower-cased on both sides: the snapshotted role is whatever the user row
        # held at write time, and older rows are not guaranteed to be normalised.
        query = query.filter(func.lower(AuditLog.actor_role) == actor_role.lower())
    if subject_employee_id:
        query = query.filter(AuditLog.subject_employee_id == subject_employee_id)
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    if date_from:
        effective_date_from = max(date_from, retention_cutoff)
        query = query.filter(AuditLog.created_at >= effective_date_from)
    if date_to:
        # date_to is inclusive: a timestamp on that day is still "<= end of day".
        query = query.filter(AuditLog.created_at < date_to + timedelta(days=1))
    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                AuditLog.actor_name.ilike(term),
                AuditLog.actor_email.ilike(term),
                AuditLog.entity_name.ilike(term),
                AuditLog.subject_name.ilike(term),
                AuditLog.summary.ilike(term),
                AuditLog.action.ilike(term),
            )
        )

    total = query.count()

    entries = (
        # id as tiebreaker so rows with identical timestamps keep a stable order
        # across pages — without it a row can appear twice and another never.
        query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    avatars = _avatars_for(db, entries)
    return {
        "items": [_serialize(e, avatars.get(e.actor_id)) for e in entries],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/stats")
def audit_log_stats(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin_only),
):
    """Headline counts for the dashboard cards.

    Computed in SQL rather than in the browser: the page only ever holds one page of
    rows, so counting client-side would report the page size, not the truth.
    """
    today = date_type.today()
    week_ago = today - timedelta(days=7)
    retention_cutoff = today - timedelta(days=RETENTION_DAYS)

    total = db.query(func.count(AuditLog.id)).filter(AuditLog.created_at >= retention_cutoff).scalar() or 0
    today_count = (
        db.query(func.count(AuditLog.id))
        .filter(AuditLog.created_at >= today)
        .scalar()
        or 0
    )
    week_count = (
        db.query(func.count(AuditLog.id))
        .filter(AuditLog.created_at >= week_ago)
        .scalar()
        or 0
    )
    # Reversals are the entries worth surfacing: someone walked back a decision.
    reversals = (
        db.query(func.count(AuditLog.id))
        .filter(
            AuditLog.created_at >= week_ago,
            AuditLog.action.in_(REVERSAL_ACTIONS),
        )
        .scalar()
        or 0
    )
    active_actors = (
        db.query(func.count(func.distinct(AuditLog.actor_id)))
        .filter(AuditLog.created_at >= week_ago)
        .scalar()
        or 0
    )

    by_category = dict(
        db.query(AuditLog.category, func.count(AuditLog.id))
        .filter(AuditLog.created_at >= retention_cutoff)
        .group_by(AuditLog.category)
        .all()
    )
    # Feeds the counts on the role tabs. Grouped on the lower-cased role so the
    # tabs match what the ``actor_role`` filter above actually selects.
    by_actor_role = dict(
        db.query(func.lower(AuditLog.actor_role), func.count(AuditLog.id))
        .filter(AuditLog.created_at >= retention_cutoff, AuditLog.actor_role.isnot(None))
        .group_by(func.lower(AuditLog.actor_role))
        .all()
    )

    return {
        "total": total,
        "today": today_count,
        "last_7_days": week_count,
        "reversals_7d": reversals,
        "active_actors_7d": active_actors,
        "by_category": by_category,
        "by_actor_role": by_actor_role,
    }


@router.get("/filters")
def audit_log_filters(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin_only),
):
    """Distinct values for the filter controls, so the UI offers only real options."""
    retention_cutoff = date_type.today() - timedelta(days=RETENTION_DAYS)
    categories = [
        c for (c,) in db.query(AuditLog.category).filter(AuditLog.created_at >= retention_cutoff).distinct().order_by(AuditLog.category)
    ]
    action_types = [
        a
        for (a,) in db.query(AuditLog.action_type)
        .filter(AuditLog.created_at >= retention_cutoff)
        .distinct()
        .order_by(AuditLog.action_type)
    ]
    actors = [
        {"id": actor_id, "name": name, "role": role}
        for actor_id, name, role in (
            db.query(AuditLog.actor_id, AuditLog.actor_name, AuditLog.actor_role)
            .filter(AuditLog.created_at >= retention_cutoff, AuditLog.actor_id.isnot(None))
            .distinct()
            .order_by(AuditLog.actor_name)
            .all()
        )
    ]
    return {
        "categories": categories,
        "action_types": action_types,
        "actors": actors,
    }


@router.get("/entity/{entity_type}/{entity_id}")
def audit_log_for_entity(
    entity_type: str,
    entity_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin_only),
):
    """Full timeline for one record — oldest first, so it reads as a story.

    Backs a "History" tab on a leave or employee detail view.
    """
    entries = (
        db.query(AuditLog)
        .filter(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        .all()
    )
    avatars = _avatars_for(db, entries)
    return {"items": [_serialize(e, avatars.get(e.actor_id)) for e in entries]}
