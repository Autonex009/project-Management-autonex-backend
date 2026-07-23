"""
Encord analytics sync.

Pulls per-user / per-day / per-stage platform time from Encord via the SDK
(`project.list_time_spent`) for every MainProject that has an `encord_project_hash`,
and upserts it into `encord_daily_time_spent`. All portal analytics read from that
table; Encord is never queried live by the API.

Auth: reads the SSH private key from env (ENCORD_SSH_KEY contents, or
ENCORD_SSH_KEY_FILE path). Region via ENCORD_DOMAIN (unset = EU/global default).
The `encord` package is imported lazily so the app boots even if it is absent.
"""
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.project import DailySheet
from app.models.encord_analytics import EncordDailyTimeSpent
from app.models.encord_activity import EncordDailyActivity

logger = logging.getLogger(__name__)

BACKFILL_DAYS = int(os.getenv("ENCORD_BACKFILL_DAYS", "90"))
_MAX_WINDOW_DAYS = 29  # Encord log endpoints cap windows; chunk backfills to be safe.


def _normalise_key() -> str | None:
    """Return the SSH private key contents with real newlines, or None."""
    raw = os.getenv("ENCORD_SSH_KEY")
    if raw:
        return raw.replace("\\n", "\n").strip() + "\n"
    return None


def _client():
    """Build an authenticated EncordUserClient. Raises RuntimeError on config/SDK errors."""
    try:
        from encord import EncordUserClient  # lazy import
    except ImportError as exc:
        raise RuntimeError("The 'encord' package is not installed (pip install encord).") from exc

    domain = os.getenv("ENCORD_DOMAIN") or None
    key_contents = _normalise_key()
    key_path = os.getenv("ENCORD_SSH_KEY_FILE") or None

    kwargs = {}
    if domain:
        kwargs["domain"] = domain
    try:
        if key_contents:
            return EncordUserClient.create_with_ssh_private_key(ssh_private_key=key_contents, **kwargs)
        if key_path:
            return EncordUserClient.create_with_ssh_private_key(ssh_private_key_path=key_path, **kwargs)
    except Exception as exc:
        raise RuntimeError(f"Encord authentication failed: {exc}") from exc
    raise RuntimeError("No Encord SSH key configured (set ENCORD_SSH_KEY or ENCORD_SSH_KEY_FILE).")


def _role_name(role) -> str | None:
    if role is None:
        return None
    return getattr(role, "name", None) or str(role)


def _stage_title(stage) -> str | None:
    if stage is None:
        return None
    return getattr(stage, "title", None) or str(stage)


def _windows(start: datetime, end: datetime):
    """Yield (start, end) chunks no longer than _MAX_WINDOW_DAYS."""
    cur = start
    step = timedelta(days=_MAX_WINDOW_DAYS)
    while cur < end:
        chunk_end = min(cur + step, end)
        yield cur, chunk_end
        cur = chunk_end


def _default_window() -> tuple[datetime, datetime]:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=1), today


def mapped_projects(db: Session) -> list[DailySheet]:
    return (
        db.query(DailySheet)
        .filter(DailySheet.encord_project_hash.isnot(None))
        .filter(DailySheet.encord_project_hash != "")
        .all()
    )


def preview(db: Session) -> dict:
    """Read-only: what the sync would target. Does NOT call Encord."""
    start, end = _default_window()
    projects = mapped_projects(db)
    return {
        "mapped_projects": [
            {"sub_project_id": p.id, "name": p.name, "encord_project_hash": p.encord_project_hash}
            for p in projects
        ],
        "count": len(projects),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
    }


def _upsert(db: Session, *, sub_project_id, project_hash, metric_date, user_email, role, stage, seconds):
    q = db.query(EncordDailyTimeSpent).filter(
        EncordDailyTimeSpent.encord_project_hash == project_hash,
        EncordDailyTimeSpent.metric_date == metric_date,
        EncordDailyTimeSpent.user_email == user_email,
    )
    q = q.filter(EncordDailyTimeSpent.workflow_stage.is_(None)) if stage is None \
        else q.filter(EncordDailyTimeSpent.workflow_stage == stage)
    row = q.first()
    if row:
        row.time_spent_seconds = seconds
        row.project_user_role = role
        row.sub_project_id = sub_project_id
        return "updated"
    db.add(EncordDailyTimeSpent(
        sub_project_id=sub_project_id,
        encord_project_hash=project_hash,
        metric_date=metric_date,
        user_email=user_email,
        project_user_role=role,
        workflow_stage=stage,
        time_spent_seconds=seconds,
    ))
    return "inserted"


def _upsert_activity(db: Session, *, sub_project_id, project_hash, metric_date, user_email,
                     tasks_submitted=0, labels_created=0, review_actions=0):
    row = (
        db.query(EncordDailyActivity)
        .filter(
            EncordDailyActivity.encord_project_hash == project_hash,
            EncordDailyActivity.metric_date == metric_date,
            EncordDailyActivity.user_email == user_email,
        )
        .first()
    )
    if row:
        row.tasks_submitted = tasks_submitted
        row.labels_created = labels_created
        row.review_actions = review_actions
        row.sub_project_id = sub_project_id
        return "updated"
    db.add(EncordDailyActivity(
        sub_project_id=sub_project_id,
        encord_project_hash=project_hash,
        metric_date=metric_date,
        user_email=user_email,
        tasks_submitted=tasks_submitted,
        labels_created=labels_created,
        review_actions=review_actions,
    ))
    return "inserted"


def _sync_project_activity(db: Session, project, sp, start: datetime, end: datetime) -> int:
    """Pull task actions + label logs for one project; upsert per (day, user). Returns row count."""
    # (day, user) -> {"tasks": n, "labels": n, "review": n}
    agg: dict[tuple, dict] = {}

    def bucket(day, email):
        return agg.setdefault((day, email), {"tasks": 0, "labels": 0, "review": 0})

    # Task actions: SUBMIT -> tasks; APPROVE/REJECT -> review actions.
    try:
        from encord.orm.analytics import TaskActionType
        for win_start, win_end in _windows(start, end):
            for act in project.get_task_actions(after=win_start, before=win_end):
                email = getattr(act, "actor_email", None)
                ts = getattr(act, "timestamp", None)
                if not email or ts is None:
                    continue
                day = ts.date()
                atype = getattr(act, "action_type", None)
                b = bucket(day, email)
                if atype == TaskActionType.SUBMIT:
                    b["tasks"] += 1
                elif atype in (TaskActionType.APPROVE, TaskActionType.REJECT):
                    b["review"] += 1
    except Exception as exc:
        logger.warning("[encord_sync] task actions for %s skipped: %s", sp.id, exc)

    # Label logs: ADD -> labels created.
    try:
        from encord.orm.label_log import Action
        for win_start, win_end in _windows(start, end):
            for log in project.get_label_logs(after=win_start, before=win_end):
                email = getattr(log, "user_email", None)
                created = getattr(log, "created_at", None)
                if not email or created is None:
                    continue
                day = created.date()
                if getattr(log, "action", None) == Action.ADD:
                    bucket(day, email)["labels"] += 1
    except Exception as exc:
        logger.warning("[encord_sync] label logs for %s skipped: %s", sp.id, exc)

    for (day, email), b in agg.items():
        _upsert_activity(
            db, sub_project_id=sp.id, project_hash=sp.encord_project_hash,
            metric_date=day, user_email=email,
            tasks_submitted=b["tasks"], labels_created=b["labels"], review_actions=b["review"],
        )
    return len(agg)


def run_sync(db: Session, start: datetime | None = None, end: datetime | None = None) -> dict:
    """Pull time-spent for all mapped projects and upsert daily rows. Returns a summary."""
    if start is None or end is None:
        start, end = _default_window()

    client = _client()
    summary = {"projects": 0, "inserted": 0, "updated": 0, "errors": 0, "details": []}

    for sp in mapped_projects(db):
        phash = sp.encord_project_hash
        try:
            project = client.get_project(phash)
            # aggregate seconds per (date, user, stage)
            agg: dict[tuple, dict] = {}
            for win_start, win_end in _windows(start, end):
                for ts in project.list_time_spent(start=win_start, end=win_end):
                    day = ts.period_start_time.date()
                    email = ts.user_email
                    stage = _stage_title(getattr(ts, "workflow_stage", None))
                    key = (day, email, stage)
                    bucket = agg.setdefault(key, {"seconds": 0, "role": _role_name(getattr(ts, "project_user_role", None))})
                    bucket["seconds"] += int(getattr(ts, "time_spent_seconds", 0) or 0)

            for (day, email, stage), bucket in agg.items():
                outcome = _upsert(
                    db, sub_project_id=sp.id, project_hash=phash, metric_date=day,
                    user_email=email, role=bucket["role"], stage=stage, seconds=bucket["seconds"],
                )
                summary[outcome] += 1

            # Also pull tasks-submitted / labels-created / review actions.
            activity_rows = _sync_project_activity(db, project, sp, start, end)

            db.commit()
            summary["projects"] += 1
            summary["details"].append({"sub_project_id": sp.id, "encord_project_hash": phash, "rows": len(agg), "activity_rows": activity_rows})
        except Exception as exc:
            db.rollback()
            summary["errors"] += 1
            summary["details"].append({"sub_project_id": sp.id, "encord_project_hash": phash, "error": str(exc)})
            logger.error("[encord_sync] project %s (%s) failed: %s", sp.id, phash, exc)

    return summary
