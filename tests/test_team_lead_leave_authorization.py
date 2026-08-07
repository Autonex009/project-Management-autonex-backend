"""End-to-end check that leave decisions respect the project hierarchy.

The unit tests in ``test_project_scope`` pin the rules; these pin that the leave endpoints
actually call them. Worth separating, because the pre-existing lifecycle tests approve as an
**admin**, which bypasses scoping — so they would keep passing even if every manager-facing
endpoint had lost its guard entirely.

Callers that share one screen:

* the project's own PM                          → 200
* a team lead on that project                   → 200 (same powers as the PM)
* a PM of a different project                   → 403
* an admin                                      → 200

And the escalation that makes it a hierarchy rather than a flat role:

* a PM asked to decide another **manager's** own leave  → 403, admin only
* a lead's own leave, asked by the project's PM         → 200
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.pop("RAZORPAY_API_KEY", None)

import sys
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.db.database as database
from app.db.database import Base

# Import every model so the in-memory schema resolves cross-table FKs.
import app.models.allocation       # noqa: F401
import app.models.employee         # noqa: F401
import app.models.guideline        # noqa: F401
import app.models.leave            # noqa: F401
import app.models.notification     # noqa: F401
import app.models.parent_project   # noqa: F401
import app.models.payroll          # noqa: F401
import app.models.project          # noqa: F401
import app.models.referral         # noqa: F401
import app.models.side_project     # noqa: F401
import app.models.signup_request   # noqa: F401
import app.models.skill            # noqa: F401
import app.models.sub_project      # noqa: F401
import app.models.user             # noqa: F401
import app.models.wfh              # noqa: F401

import app.models.audit_log         # noqa: F401

from app.api.leaves import router as leave_router
from app.models.allocation import Allocation
from app.models.audit_log import AuditLog
from app.models.employee import Employee
from app.models.leave import Leave
from app.models.project import DailySheet
from app.models.user import User
from app.services.auth_service import get_current_user


# Mutable holder so each test can choose who is calling.
_CALLER = {}


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    api = FastAPI()
    api.include_router(leave_router)
    api.dependency_overrides[database.get_db] = override_get_db
    api.dependency_overrides[get_current_user] = lambda: _CALLER["user"]

    db = TestingSessionLocal()
    seeded = _seed(db)
    yield TestClient(api), db, seeded
    db.close()
    Base.metadata.drop_all(bind=engine)


def _seed(db):
    """Two projects with different PMs, one worker on project A, one pure team lead."""
    pm_a = Employee(name="PM A", email="pma@x.com", status="active",
                    employee_type="Full-time", designation="Program Manager")
    pm_b = Employee(name="PM B", email="pmb@x.com", status="active",
                    employee_type="Full-time", designation="Program Manager")
    lead = Employee(name="Lead", email="lead@x.com", status="active",
                    employee_type="Full-time", designation="Team Lead")
    worker = Employee(name="Worker", email="worker@x.com", status="active",
                      employee_type="Full-time", designation="Annotator/ Reviewer")
    admin = Employee(name="Admin", email="admin@x.com", status="active",
                     employee_type="Full-time", designation="Admin")
    db.add_all([pm_a, pm_b, lead, worker, admin])
    db.commit()
    for e in (pm_a, pm_b, lead, worker, admin):
        db.refresh(e)

    sheet_common = dict(client="Acme", project_type="Full", total_tasks=10,
                        estimated_time_per_task=1.0, start_date=date(2026, 1, 1))
    project_a = DailySheet(name="Project A", assigned_employee_ids=[pm_a.id], **sheet_common)
    project_b = DailySheet(name="Project B", assigned_employee_ids=[pm_b.id], **sheet_common)
    db.add_all([project_a, project_b])
    db.commit()
    db.refresh(project_a)
    db.refresh(project_b)

    db.add_all([
        Allocation(employee_id=worker.id, sub_project_id=project_a.id,
                   role_tags=["Annotation"]),
        # A pure team lead on the same project as the worker.
        Allocation(employee_id=lead.id, sub_project_id=project_a.id,
                   role_tags=["Team Lead"]),
    ])

    users = {}
    for key, emp, role in (
        ("pm_a", pm_a, "pm"),
        ("pm_b", pm_b, "pm"),
        ("lead", lead, "team_lead"),
        ("admin", admin, "admin"),
    ):
        u = User(email=emp.email, password_hash="x", name=emp.name, role=role,
                 employee_id=emp.id, is_active=True)
        db.add(u)
        users[key] = u
    db.commit()
    for u in users.values():
        db.refresh(u)

    return {"users": users, "worker": worker, "project_a": project_a}


def _pending_leave(db, employee_id):
    """A pending, unflagged leave so nothing but authorisation can reject it."""
    leave = Leave(
        employee_id=employee_id,
        # Must be one of the values LeaveSchema accepts, or the list endpoint fails
        # serialising the row rather than returning it.
        leave_type="casual_sick",
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 2),
        reason="Personal",
        status="pending",
        flagged=False,
    )
    db.add(leave)
    db.commit()
    db.refresh(leave)
    return leave


def _as(seeded, key):
    _CALLER["user"] = seeded["users"][key]


# ── Approve ─────────────────────────────────────────────────────────

def test_project_pm_can_approve_their_own_members_leave(env):
    """The case the admin-only lifecycle tests never exercised."""
    client, db, seeded = env
    _as(seeded, "pm_a")
    leave = _pending_leave(db, seeded["worker"].id)

    resp = client.patch(f"/api/leaves/{leave.id}/approve")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    db.expire_all()
    assert db.query(Leave).filter(Leave.id == leave.id).first().status == "approved"


def test_pm_of_another_project_is_refused(env):
    client, db, seeded = env
    _as(seeded, "pm_b")
    leave = _pending_leave(db, seeded["worker"].id)

    resp = client.patch(f"/api/leaves/{leave.id}/approve")

    assert resp.status_code == 403
    db.expire_all()
    assert db.query(Leave).filter(Leave.id == leave.id).first().status == "pending"


def test_team_lead_on_the_same_project_may_approve(env):
    """A lead decides for their own team, exactly as the project's PM would."""
    client, db, seeded = env
    _as(seeded, "lead")
    leave = _pending_leave(db, seeded["worker"].id)

    resp = client.patch(f"/api/leaves/{leave.id}/approve")

    assert resp.status_code == 200, resp.text
    db.expire_all()
    assert db.query(Leave).filter(Leave.id == leave.id).first().status == "approved"


def test_admin_is_unaffected(env):
    client, db, seeded = env
    _as(seeded, "admin")
    leave = _pending_leave(db, seeded["worker"].id)

    assert client.patch(f"/api/leaves/{leave.id}/approve").status_code == 200


# ── The other transitions must be guarded too ───────────────────────

@pytest.mark.parametrize(
    "path,initial",
    [
        ("reject", "pending"),
        ("undo-approve", "approved"),
        ("undo-reject", "rejected"),
    ],
)
def test_every_decision_transition_refuses_an_outside_pm(env, path, initial):
    """A guard on approve alone would leave reject and the undos wide open.

    Asked by a PM of a different project, which is the caller scoping must still refuse now
    that a lead is allowed.
    """
    client, db, seeded = env
    _as(seeded, "pm_b")
    leave = _pending_leave(db, seeded["worker"].id)
    leave.status = initial
    db.commit()

    resp = client.patch(f"/api/leaves/{leave.id}/{path}")

    assert resp.status_code == 403, f"{path} was not guarded"
    db.expire_all()
    assert db.query(Leave).filter(Leave.id == leave.id).first().status == initial


def test_a_team_leads_own_leave_is_approved_by_the_project_pm(env):
    """Requirement: a team lead's own leave goes to their project's PM."""
    client, db, seeded = env
    lead_employee_id = seeded["users"]["lead"].employee_id
    _as(seeded, "pm_a")
    leave = _pending_leave(db, lead_employee_id)

    resp = client.patch(f"/api/leaves/{leave.id}/approve")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"


def test_a_peer_lead_may_not_approve_another_leads_leave(env):
    """Requests go up, not sideways — otherwise two leads could sign each other off.

    Both leads sit on project A and both may act on it, so only the tier rule separates
    them.
    """
    client, db, seeded = env
    peer = Employee(name="Peer Lead", email="peer@x.com", status="active",
                    employee_type="Full-time", designation="Team Lead")
    db.add(peer)
    db.commit()
    db.refresh(peer)
    db.add(Allocation(employee_id=peer.id, sub_project_id=seeded["project_a"].id,
                      role_tags=["Team Lead"]))
    peer_user = User(email=peer.email, password_hash="x", name=peer.name,
                     role="team_lead", employee_id=peer.id, is_active=True)
    db.add(peer_user)
    db.commit()

    leave = _pending_leave(db, seeded["users"]["lead"].employee_id)
    _CALLER["user"] = peer_user
    resp = client.patch(f"/api/leaves/{leave.id}/approve")

    assert resp.status_code == 403
    assert "program manager" in resp.json()["detail"]
    db.expire_all()
    assert db.query(Leave).filter(Leave.id == leave.id).first().status == "pending"


def test_a_program_manager_acting_as_team_lead_escalates_to_admin(env):
    """A manager's own request stays with an admin even on someone else's project.

    PM B is allocated to project A, which PM A manages — the designation rule is what stops
    PM A approving a fellow manager's leave.
    """
    client, db, seeded = env
    pm_b_employee_id = seeded["users"]["pm_b"].employee_id
    db.add(Allocation(employee_id=pm_b_employee_id,
                      sub_project_id=seeded["project_a"].id,
                      role_tags=["Team Lead"]))
    db.commit()

    _as(seeded, "pm_a")
    leave = _pending_leave(db, pm_b_employee_id)
    resp = client.patch(f"/api/leaves/{leave.id}/approve")
    assert resp.status_code == 403, "a project PM must not approve a PM's own leave"
    assert "admin" in resp.json()["detail"].lower()

    # ...and the admin still can.
    _as(seeded, "admin")
    assert client.patch(f"/api/leaves/{leave.id}/approve").status_code == 200


# ── Attribution ─────────────────────────────────────────────────────

@pytest.mark.parametrize("caller", ["pm_a", "lead", "admin"])
def test_the_audit_entry_names_whoever_actually_decided(env, caller):
    """Several people may be entitled to decide one request, so the trail must say which.

    That is what allows the rule to stay permissive — any manager of any project the person
    is on — without losing accountability.
    """
    client, db, seeded = env
    actor = seeded["users"][caller]
    _as(seeded, caller)
    leave = _pending_leave(db, seeded["worker"].id)

    assert client.patch(f"/api/leaves/{leave.id}/approve").status_code == 200

    db.expire_all()
    entry = (
        db.query(AuditLog)
        .filter(AuditLog.action == "leave.approved", AuditLog.entity_id == leave.id)
        .first()
    )
    assert entry is not None, "approving a leave must leave an audit entry"
    assert entry.actor_id == actor.id
    assert entry.actor_name == actor.name
    assert entry.actor_role == actor.role
    # ...and the row itself carries the decider, for the "approved by" line in the UI.
    assert db.query(Leave).filter(Leave.id == leave.id).first().approved_by == actor.id


# ── Reading the team's requests ─────────────────────────────────────

def test_team_lead_can_list_other_peoples_leaves(env):
    """A team lead must receive the whole team's leaves, not just its own.

    GET /api/leaves narrows the query to the caller's own records for anyone without
    team-wide read. Omitting team_lead there fails silently in the worst way: the request
    still returns 200, so the page renders "No leave requests" and looks like a scoping
    bug in the UI rather than a role missing from a list on the server.
    """
    client, db, seeded = env
    worker_id = seeded["worker"].id
    _pending_leave(db, worker_id)

    _as(seeded, "lead")
    resp = client.get("/api/leaves")

    assert resp.status_code == 200, resp.text
    returned_employee_ids = {row["employee_id"] for row in resp.json()}
    assert worker_id in returned_employee_ids, (
        "team lead got only its own leaves — check has_team_read covers team_lead"
    )


def test_a_plain_employee_still_only_sees_their_own_leaves(env):
    """The guard the above must not loosen."""
    client, db, seeded = env
    worker_id = seeded["worker"].id
    _pending_leave(db, worker_id)

    lead_user = seeded["users"]["lead"]
    _CALLER["user"] = User(
        id=999, email="plain@x.com", name="Plain", password_hash="x",
        role="employee", employee_id=lead_user.employee_id, is_active=True,
    )
    resp = client.get("/api/leaves")

    assert resp.status_code == 200, resp.text
    assert worker_id not in {row["employee_id"] for row in resp.json()}


def test_authorisation_is_checked_before_the_flagged_remark_rule(env):
    """A refused caller must not learn that a remark would have let them through."""
    client, db, seeded = env
    _as(seeded, "pm_b")   # manages a different project
    leave = _pending_leave(db, seeded["worker"].id)
    leave.flagged = True
    db.commit()

    resp = client.patch(f"/api/leaves/{leave.id}/approve")

    assert resp.status_code == 403  # not 422
