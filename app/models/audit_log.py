"""Audit log — an append-only record of who did what, to whom, and when.

Two deliberate design choices worth knowing before you change this table:

1. **Actor identity is snapshotted, not joined.** ``actor_name`` / ``actor_email`` /
   ``actor_role`` are copied onto the row at write time instead of being read from
   ``users`` on every query. An audit trail has to say what was true *at the time*:
   if a PM is later promoted to admin, joining ``users`` would retroactively relabel
   every one of their old actions as "admin". Snapshotting also means the entry
   survives the user row being removed.

   ``avatar_url`` is deliberately *not* snapshotted — a profile photo is cosmetic,
   not an audit fact, so it is resolved at read time and always shows the current one.

2. **Nothing here is ever updated or deleted by the app.** There are no update paths
   and no DELETE endpoint. Retention trimming, if it ever happens, is a separate
   offline job. Treat rows as immutable.
"""
from sqlalchemy import (
    Column, ForeignKey, Index, Integer, JSON, String, Text, TIMESTAMP,
)
from sqlalchemy.sql import func

from app.db.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)

    # ── Who performed the action ────────────────────────────────────────
    # Always derived from the authenticated session, never from client input.
    # Null means the actor was the system itself (scheduler, sync job, migration).
    actor_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_name = Column(String(255), nullable=True)
    actor_email = Column(String(255), nullable=True)
    actor_role = Column(String(30), nullable=True)

    # ── What happened ───────────────────────────────────────────────────
    # ``action`` is the stable machine key we filter and report on ("leave.approved").
    # ``action_type`` and ``category`` are the display facets the Change Log page
    # already renders as badges and filter pills — keep their vocabularies in sync
    # with the frontend rather than inventing new values ad hoc.
    action = Column(String(60), nullable=False)
    action_type = Column(String(20), nullable=False)
    category = Column(String(30), nullable=False)

    # ── What was acted upon ─────────────────────────────────────────────
    # ``entity_name`` is snapshotted so the log renders without extra lookups and
    # still reads correctly after the underlying record is renamed or removed.
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(Integer, nullable=True)
    entity_name = Column(String(255), nullable=True)

    # ── Who it was done to ──────────────────────────────────────────────
    # Distinct from the actor: "admin approved *Bhairavi's* leave". Points at
    # employees (not users) because that is who the business events are about;
    # for self-directed events like login, actor and subject are the same person.
    subject_employee_id = Column(
        Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    subject_name = Column(String(255), nullable=True)

    # ── The change itself ───────────────────────────────────────────────
    # ``details`` holds field-level diffs shaped exactly as the UI expects:
    #   [{"field": "Status", "from": "pending", "to": "approved"}, ...]
    # Never put decrypted salary figures in here — log that pay changed, not to what.
    details = Column(JSON, nullable=True)

    # A pre-rendered sentence ("Approved casual leave for Bhairavi D., 12–14 Aug").
    # Written once at record time so the frontend needs no per-action formatting
    # logic, and so old entries keep reading correctly after wording changes.
    summary = Column(Text, nullable=True)

    # ── Request context ─────────────────────────────────────────────────
    ip = Column(String(45), nullable=True)          # 45 chars fits IPv6
    user_agent = Column(String(255), nullable=True)

    created_at = Column(TIMESTAMP, server_default=func.now(), nullable=False)

    # Indexes are declared here *and* repeated in the Alembic migration on purpose:
    # app.main still calls Base.metadata.create_all(), so a fresh database may be
    # built by either path and both need to produce the same physical table.
    __table_args__ = (
        # Default view: newest first. ``id`` breaks ties so that two rows written in
        # the same millisecond can never swap places between page 1 and page 2 —
        # without it, paginated reads can show a duplicate and skip another row.
        Index("ix_audit_logs_created_at_id", created_at.desc(), id.desc()),
        # "What did this person do?" Filter column first, sort column second —
        # reversed, the index cannot serve this query.
        Index("ix_audit_logs_actor_created", "actor_id", created_at.desc()),
        # "What was done to this person?"
        Index("ix_audit_logs_subject_created", "subject_employee_id", created_at.desc()),
        # Full history of one leave / employee, for a detail-view timeline.
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        # Backs the category filter pills.
        Index("ix_audit_logs_category_created", "category", created_at.desc()),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<AuditLog {self.id} {self.action} "
            f"actor={self.actor_id} entity={self.entity_type}:{self.entity_id}>"
        )
