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


def _normalise_key(env_name: str) -> str | None:
    """Return the SSH private key contents from env `env_name` with real newlines, or None."""
    raw = os.getenv(env_name)
    if raw:
        return raw.replace("\\n", "\n").strip() + "\n"
    return None


# Encord regions. Each region is a separate Encord deployment with its own API
# domain and its own SSH key — a key only sees projects in its region. We build a
# client per configured region and, for each project, use whichever region's client
# can actually see it (see `run_sync`). US default domain per Encord's US deployment.
_REGIONS = (
    # (label, key-env, key-file-env, domain-env, default-domain)
    ("EU", "ENCORD_SSH_KEY", "ENCORD_SSH_KEY_FILE", "ENCORD_DOMAIN", None),
    ("US", "ENCORD_SSH_KEY_US", "ENCORD_SSH_KEY_US_FILE", "ENCORD_DOMAIN_US", "https://api.us.encord.com"),
)


def _build_client(key_env: str, key_file_env: str, domain_env: str, default_domain: str | None):
    """Build one EncordUserClient for a region, or None if that region isn't configured."""
    from encord import EncordUserClient  # lazy import (validated by caller)

    key_contents = _normalise_key(key_env)
    key_path = os.getenv(key_file_env) or None
    if not key_contents and not key_path:
        return None

    domain = os.getenv(domain_env) or default_domain
    kwargs = {"domain": domain} if domain else {}
    if key_contents:
        return EncordUserClient.create_with_ssh_private_key(ssh_private_key=key_contents, **kwargs)
    return EncordUserClient.create_with_ssh_private_key(ssh_private_key_path=key_path, **kwargs)


def _region_clients():
    """Return [(region_label, client), ...] for every configured Encord region."""
    try:
        from encord import EncordUserClient  # noqa: F401  (ensure the package is present)
    except ImportError as exc:
        raise RuntimeError("The 'encord' package is not installed (pip install encord).") from exc

    clients = []
    for label, key_env, key_file_env, domain_env, default_domain in _REGIONS:
        try:
            client = _build_client(key_env, key_file_env, domain_env, default_domain)
        except Exception as exc:
            raise RuntimeError(f"Encord authentication failed for region {label}: {exc}") from exc
        if client is not None:
            clients.append((label, client))

    if not clients:
        raise RuntimeError("No Encord SSH key configured (set ENCORD_SSH_KEY and/or ENCORD_SSH_KEY_US).")
    return clients


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

    clients = _region_clients()   # [(region, client), ...] across all configured regions
    summary = {"projects": 0, "inserted": 0, "updated": 0, "errors": 0, "details": []}

    for sp in mapped_projects(db):
        phash = sp.encord_project_hash
        # Find which region can actually see this project. Try each region's client;
        # the first that returns the project wins. If none can, the hash is invalid.
        project = None
        region = None
        resolve_errors = []
        for reg, client in clients:
            try:
                project = client.get_project(phash)
                region = reg
                break
            except Exception as exc:
                resolve_errors.append(f"{reg}: {exc}")
        if project is None:
            db.rollback()
            summary["errors"] += 1
            summary["details"].append({
                "sub_project_id": sp.id,
                "encord_project_hash": phash,
                "error": "project not found in any region (" + "; ".join(resolve_errors) + ")",
            })
            logger.error("[encord_sync] project %s (%s) not found in any region: %s", sp.id, phash, resolve_errors)
            continue

        try:
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
            summary["details"].append({"sub_project_id": sp.id, "encord_project_hash": phash, "region": region, "rows": len(agg), "activity_rows": activity_rows})
        except Exception as exc:
            db.rollback()
            summary["errors"] += 1
            summary["details"].append({"sub_project_id": sp.id, "encord_project_hash": phash, "error": str(exc)})
            logger.error("[encord_sync] project %s (%s) failed: %s", sp.id, phash, exc)

    return summary


def get_period_dates(period: str, custom_month: str = None) -> tuple[datetime, datetime]:
    """Calculate the start and end datetime for the requested period (Current Month, Last Month, Custom Month)."""
    now = datetime.now()
    if period == "current_month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now
    elif period == "last_month":
        first_of_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = first_of_this - timedelta(seconds=1)
        start = end.replace(day=1, hour=0, minute=0, second=0)
    elif period == "custom" and custom_month:
        try:
            # custom_month format: "YYYY-MM"
            dt = datetime.strptime(custom_month, "%Y-%m")
            start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            # Find the end of that month
            next_month = start.replace(day=28) + timedelta(days=4)
            end = next_month - timedelta(days=next_month.day)
            end = end.replace(hour=23, minute=59, second=59)
            if end > now:
                end = now
        except ValueError:
            raise ValueError("custom_month must be YYYY-MM")
    else:
        raise ValueError("Invalid period")
    return start, end


def run_user_sync(db: Session, log_id: str, employee_id: int, start: datetime, end: datetime):
    """
    Historical sync for a specific user over a specific time range.
    Only fetches data for the missing days for that user.
    """
    from app.models.employee import Employee
    from app.models.encord_analytics import EncordSyncLog
    import time
    
    log = db.query(EncordSyncLog).filter_by(id=log_id).first()
    if not log:
        return {"error": "Log not found"}

    try:
        emp = db.query(Employee).filter_by(id=employee_id).first()
        if not emp or not emp.encord_id:
            raise ValueError("Employee not found or lacks encord_id")

        email = emp.encord_id
        clients = _region_clients()
        projects = mapped_projects(db)
        
        upserted = 0
        
        # Loop through projects
        for sp in projects:
            phash = sp.encord_project_hash
            
            # Find which region has the project
            project = None
            for reg, client in clients:
                try:
                    project = client.get_project(phash)
                    break
                except Exception:
                    pass
                    
            if not project:
                continue
                
            # Chunk the time range so we don't hit Encord's limits
            agg = {}
            for win_start, win_end in _windows(start, end):
                try:
                    for ts in project.list_time_spent(start=win_start, end=win_end):
                        # Filter down to just this user
                        if not ts.user_email or ts.user_email.strip().lower() != email.strip().lower():
                            continue
                            
                        day = ts.period_start_time.date()
                        stage = _stage_title(getattr(ts, "workflow_stage", None))
                        role = _role_name(getattr(ts, "project_user_role", None))
                        
                        key = (day, stage, role)
                        bucket = agg.setdefault(key, 0)
                        agg[key] += int(getattr(ts, "time_spent_seconds", 0) or 0)
                        
                except Exception as e:
                    logger.warning(f"Error fetching for {phash}: {e}")
                    
            for (day, stage, role), total_seconds in agg.items():
                outcome = _upsert(
                    db, sub_project_id=sp.id, project_hash=phash, metric_date=day,
                    user_email=email, role=role, stage=stage, seconds=total_seconds
                )
                if outcome in ("inserted", "updated"):
                    upserted += 1

        # Update log on success
        log.status = "success"
        log.records_upserted = upserted
        log.completed_at = datetime.now()
        db.commit()
        return {"status": "success", "upserted": upserted}
        
    except Exception as e:
        db.rollback()
        log = db.query(EncordSyncLog).filter_by(id=log_id).first()
        if log:
            log.status = "failed"
            log.completed_at = datetime.now()
            db.commit()
        logger.error(f"[encord_sync] user sync failed: {e}")
        return {"status": "failed", "error": str(e)}

