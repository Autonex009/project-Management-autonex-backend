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
            db.commit()
            summary["projects"] += 1
            summary["details"].append({"sub_project_id": sp.id, "encord_project_hash": phash, "rows": len(agg)})
        except Exception as exc:
            db.rollback()
            summary["errors"] += 1
            summary["details"].append({"sub_project_id": sp.id, "encord_project_hash": phash, "error": str(exc)})
            logger.error("[encord_sync] project %s (%s) failed: %s", sp.id, phash, exc)

    return summary
