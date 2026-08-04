"""Recording side of the audit log.

Call :func:`record` from an endpoint right after a meaningful action succeeds.
Design rules, all of which matter in production:

* **Same session, no commit.** The entry is added to the caller's ``Session`` and
  rides along on whatever commit the caller already makes. If the business action
  rolls back, so does its log entry — you never get a recorded "approved" for an
  approval that actually failed.

* **Never break the request.** Every failure here is swallowed and logged. A broken
  audit write must not turn a working leave approval into a 500.

* **Actor comes from the session, never from the client.** Pass the ``User`` resolved
  by ``get_current_user`` / ``require_role``. Anything the browser supplied about who
  acted is untrustworthy by definition.

* **Capture ``details`` before mutating.** Read the old values off the row *before*
  assigning new ones, otherwise the "from" side of the diff is already the new value.

* **Never log decrypted salary.** Record that pay changed and by whom, not the figures.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.user import User

logger = logging.getLogger(__name__)


def field_diff(label: str, before: Any, after: Any) -> Optional[dict]:
    """Build one entry for ``details``, or ``None`` if the value did not change.

    Feed the results through :func:`changes` so unchanged fields drop out and the
    log shows only what actually moved.
    """
    if before == after:
        return None
    return {
        "field": label,
        "from": _readable(before),
        "to": _readable(after),
    }


def changes(*diffs: Optional[dict]) -> list[dict]:
    """Drop the ``None`` entries produced by :func:`field_diff`."""
    return [d for d in diffs if d]


def snapshot(obj: Any, keys) -> dict:
    """Copy the current value of ``keys`` off ``obj``.

    Call this *before* applying an update, then pass the result to :func:`diff_all`.
    Reading the values afterwards would compare each new value against itself, which
    is how you end up with useless "Designation: Previous → Updated" entries.
    """
    return {key: getattr(obj, key, None) for key in keys}


def diff_all(before: dict, obj: Any, labels: Optional[dict] = None) -> list[dict]:
    """Diff every key in ``before`` against the same attribute on ``obj`` now.

    Unchanged fields drop out, so the entry lists only what actually moved. ``labels``
    maps column names to display names; anything unmapped falls back to a humanised
    version of the column name rather than being silently skipped.
    """
    labels = labels or {}
    return changes(
        *[
            field_diff(
                labels.get(key, key.replace("_", " ").capitalize()),
                old_value,
                getattr(obj, key, None),
            )
            for key, old_value in before.items()
        ]
    )


def _readable(value: Any) -> str:
    """Render a value for display. ``None`` becomes an em dash to match the UI."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _client_ip(request: Optional[Request]) -> Optional[str]:
    """Best-effort caller IP.

    Both deploy targets sit behind a proxy, so ``request.client.host`` is the proxy.
    Prefer the first hop in X-Forwarded-For, which is the original client.
    """
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host[:45] if request.client else None


def record(
    db: Session,
    *,
    actor: Optional[User],
    action: str,
    category: str,
    action_type: str,
    entity_type: str,
    entity_id: Optional[int] = None,
    entity_name: Optional[str] = None,
    subject_employee_id: Optional[int] = None,
    subject_name: Optional[str] = None,
    details: Optional[Sequence[dict]] = None,
    summary: Optional[str] = None,
    request: Optional[Request] = None,
) -> None:
    """Append one audit entry to ``db``. Does not commit — the caller does.

    ``actor`` may be ``None`` for system-initiated work (scheduler, sync jobs).

    Args:
        action: stable machine key, e.g. ``"leave.approved"``. Filtered and reported
            on, so keep it stable once shipped.
        category: display facet matching the Change Log page's filter pills
            ("Leaves", "Employees", "Projects", "Allocations", "System").
        action_type: display facet driving the badge colour ("Applied", "Approved",
            "Rejected", "Created", "Updated", "Archived", "Restored", "Promoted").
        entity_type / entity_id: what was acted on, e.g. ``("leave", 45)``.
        entity_name: human label for the entity, snapshotted.
        subject_employee_id / subject_name: the person the action was *about*, when
            that differs from the actor.
        details: field-level diffs, ideally built with :func:`field_diff` +
            :func:`changes`.
        summary: one-line sentence rendered at write time.
    """
    try:
        entry = AuditLog(
            actor_id=actor.id if actor else None,
            actor_name=actor.name if actor else None,
            actor_email=actor.email if actor else None,
            actor_role=actor.role if actor else None,
            action=action,
            action_type=action_type,
            category=category,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name[:255] if entity_name else None,
            subject_employee_id=subject_employee_id,
            subject_name=subject_name[:255] if subject_name else None,
            details=list(details) if details else None,
            summary=summary,
            ip=_client_ip(request),
            user_agent=(
                request.headers.get("user-agent", "")[:255] if request else None
            ),
        )
        db.add(entry)
    except Exception:
        # Deliberately swallowed: losing an audit entry is bad, failing the user's
        # action because of it is worse. The traceback goes to the app logs.
        logger.exception(
            "Failed to record audit entry action=%s entity=%s:%s",
            action, entity_type, entity_id,
        )
