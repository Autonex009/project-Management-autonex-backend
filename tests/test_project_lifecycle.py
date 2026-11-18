"""
Comprehensive Verification Tests for Critical Issue 16:
Project Lifecycle State Machine, Non-Destructive Archival Architecture, and Tab Taxonomy.

Verifies:
1. Canonical Status Taxonomy & Predicates (app/constants/project_constants.py)
2. Tab Precedence Invariant (Archived > Development > Active)
3. KPI Non-Overlapping Tab Partitioning
4. Intelligent Allocation Pruning (Release workers, preserve PMs and Team Leads)
5. Allocation Restoration on Project Reactivation
6. Hard Delete Deprecation Guardrail (HTTP 400 if historical allocations exist)
"""
import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db.database import Base
from app.models.daily_checkin import DailyCheckIn

# Configure SQLite compatibility for models
DailyCheckIn.__table__.c.id.autoincrement = False

import app.models.allocation       # noqa: F401
import app.models.audit_log        # noqa: F401
import app.models.employee         # noqa: F401
import app.models.guideline        # noqa: F401
import app.models.leave            # noqa: F401
import app.models.notification     # noqa: F401
import app.models.parent_project   # noqa: F401
import app.models.payroll          # noqa: F401
import app.models.perf_eval        # noqa: F401
import app.models.project          # noqa: F401
import app.models.referral         # noqa: F401
import app.models.side_project     # noqa: F401
import app.models.signup_request   # noqa: F401
import app.models.skill            # noqa: F401
import app.models.sub_project      # noqa: F401
import app.models.user             # noqa: F401
import app.models.wfh              # noqa: F401

from app.api.projects import (
    _get_filtered_enriched_projects,
    delete_project,
    get_projects_kpi,
    update_project,
)
from app.constants.project_constants import (
    ACTIVE_STATUSES,
    ARCHIVED_STATUSES,
    TEAM_LEAD_ROLE_TAG,
    is_project_active,
    is_project_archived,
    is_project_development,
    normalize_project_status,
)
from app.models.allocation import Allocation
from app.models.employee import Employee
from app.models.parent_project import MainProject
from app.models.project import DailySheet as Project
from app.models.user import User
from app.schemas.project import ProjectUpdate


class DummyRequest:
    """Mock Starlette Request without MagicMock types that SQLite cannot bind."""
    headers = {}
    client = None


def make_project(
    name="Test Project",
    client="Acme",
    project_type="Full",
    project_status="active",
    **kwargs,
) -> Project:
    """Helper to instantiate a Project with all required non-null columns."""
    defaults = {
        "total_tasks": 10,
        "estimated_time_per_task": 0.5,
        "start_date": date(2026, 1, 1),
    }
    defaults.update(kwargs)
    return Project(
        name=name,
        client=client,
        project_type=project_type,
        project_status=project_status,
        **defaults,
    )


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture()
def db_session():
    """Isolated in-memory SQLite session with all models pre-created."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def seed_users_and_employees(db_session):
    """Seed test Admin, PM, Team Lead, and Annotator users/employees."""
    roles_data = [
        ("admin", "Admin User", "admin@company.com", "Admin"),
        ("pm", "Project Manager", "pm@company.com", "Program Manager"),
        ("tl", "Team Lead", "tl@company.com", "Team Lead"),
        ("worker", "Worker Annotator", "worker@company.com", "Annotator"),
    ]
    created = {}
    for key, name, email, desig in roles_data:
        emp = Employee(
            name=name,
            email=email,
            status="active",
            employee_type="Full-time",
            designation=desig,
        )
        db_session.add(emp)
        db_session.commit()
        db_session.refresh(emp)

        user = User(
            email=email,
            password_hash="test-hash",
            name=name,
            role=key if key in ("admin", "pm", "team_lead") else "user",
            employee_id=emp.id,
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        created[key] = {"employee": emp, "user": user}

    return created


# ── 1. Canonical Status Taxonomy & Predicates ─────────────────────────────

def test_canonical_status_taxonomy_predicates():
    """Verify active vs archived predicate classifications and normalization."""
    # Active bucket includes POC and In Progress
    assert is_project_active("poc") is True
    assert is_project_active("POC") is True
    assert is_project_active("in progress") is True
    assert is_project_active("In Progress") is True
    assert is_project_active("in-progress") is True
    assert is_project_active("active") is True
    assert is_project_active(None) is True  # defaults to active
    assert is_project_active("completed") is False
    assert is_project_active("on-hold") is False

    # Archived bucket includes Completed, On Hold, Cancelled
    assert is_project_archived("completed") is True
    assert is_project_archived("COMPLETED") is True
    assert is_project_archived("on-hold") is True
    assert is_project_archived("on_hold") is True
    assert is_project_archived("cancelled") is True
    assert is_project_archived("archived") is True
    assert is_project_archived("poc") is False
    assert is_project_archived("active") is False
    assert is_project_archived(None) is False

    # Development type classification
    assert is_project_development({"Development": "Full Stack"}) is True
    assert is_project_development(["Development"]) is True
    assert is_project_development("Development") is True
    assert is_project_development(None) is False
    assert is_project_development({}) is False
    assert is_project_development({"Training": "Onboarding"}) is False


# ── 2. Tab Precedence Invariant (Archived > Dev > Active) ──────────────────

def test_tab_precedence_routing_and_poc_placement(db_session, seed_users_and_employees):
    """
    Verify strict tab precedence:
    - POC project (Samsara) displays in Active tab.
    - Active Development project displays in Development tab.
    - Completed Development project (Verifiers) routes to Archived tab (NOT Development).
    """
    admin_user = seed_users_and_employees["admin"]["user"]

    # 1. Samsara: POC annotation project
    samsara = make_project(
        name="Samsara POC",
        client="Samsara Client",
        project_status="poc",
        project_types={"Data Modalities": "Image (RGB)"},
    )
    # 2. Active Engineering project
    dev_active = make_project(
        name="Backend Core Service",
        client="Internal",
        project_status="in progress",
        project_types={"Development": "API"},
    )
    # 3. Verifiers: Completed engineering project
    verifiers_completed = make_project(
        name="Verifiers Tool",
        client="Internal",
        project_status="completed",
        project_types={"Development": "Frontend"},
    )
    # 4. Standard Completed annotation project
    anno_completed = make_project(
        name="Old Annotation Project",
        client="Client B",
        project_status="completed",
        project_types={"Data Modalities": "Video"},
    )

    db_session.add_all([samsara, dev_active, verifiers_completed, anno_completed])
    db_session.commit()

    # Query Active Tab
    active_items = _get_filtered_enriched_projects(
        db_session,
        admin_user,
        project_view="active",
        search=None,
        main_project_id=None,
        status=None,
        priority=None,
        organization=None,
        pm_id=None,
        team_lead_id=None,
        recommendation=None,
        autonex_only=False,
    )
    active_ids = {p.id for p in active_items}
    assert samsara.id in active_ids, "Samsara (POC) must be in Active tab"
    assert dev_active.id not in active_ids, "Dev project must not be in Active tab"
    assert verifiers_completed.id not in active_ids, "Completed project must not be in Active tab"

    # Query Development Tab
    dev_items = _get_filtered_enriched_projects(
        db_session,
        admin_user,
        project_view="development",
        search=None,
        main_project_id=None,
        status=None,
        priority=None,
        organization=None,
        pm_id=None,
        team_lead_id=None,
        recommendation=None,
        autonex_only=False,
    )
    dev_ids = {p.id for p in dev_items}
    assert dev_active.id in dev_ids, "Active dev project must be in Development tab"
    assert verifiers_completed.id not in dev_ids, (
        "Strict Precedence Violation: Completed dev project must NOT leak into Development tab"
    )

    # Query Archived Tab
    archived_items = _get_filtered_enriched_projects(
        db_session,
        admin_user,
        project_view="archived",
        search=None,
        main_project_id=None,
        status=None,
        priority=None,
        organization=None,
        pm_id=None,
        team_lead_id=None,
        recommendation=None,
        autonex_only=False,
    )
    archived_ids = {p.id for p in archived_items}
    assert verifiers_completed.id in archived_ids, (
        "Completed dev project must be classified into Archived tab"
    )
    assert anno_completed.id in archived_ids, "Completed annotation project must be in Archived tab"
    assert samsara.id not in archived_ids, "POC project must not be in Archived tab"


# ── 3. KPI Tab Non-Overlapping Partition ──────────────────────────────────

def test_kpi_tab_counts_partition(db_session, seed_users_and_employees):
    """Verify get_projects_kpi tab counts form a non-overlapping partition."""
    admin_user = seed_users_and_employees["admin"]["user"]

    p_active = make_project(name="P1", project_status="active", project_types=None)
    p_poc = make_project(name="P2", project_status="poc", project_types=None)
    p_dev = make_project(name="P3", project_status="in progress", project_types={"Development": "Service"})
    p_archived1 = make_project(name="P4", project_status="completed", project_types=None)
    p_archived2 = make_project(name="P5", project_status="on-hold", project_types={"Development": "App"})
    p_archived3 = make_project(name="P6", project_status="cancelled", project_types=None)

    db_session.add_all([p_active, p_poc, p_dev, p_archived1, p_archived2, p_archived3])
    db_session.commit()

    kpi = get_projects_kpi(
        db=db_session,
        current_user=admin_user,
    )

    tab_counts = kpi["tab_counts"]
    # active tab: P1 and P2 (POC) -> 2
    assert tab_counts["active"] == 2
    # development tab: P3 -> 1
    assert tab_counts["development"] == 1
    # archived tab: P4, P5 (on-hold dev), P6 -> 3
    assert tab_counts["archived"] == 3

    # Total non-overlapping sum equals 6
    assert tab_counts["active"] + tab_counts["development"] + tab_counts["archived"] == 6


# ── 4. Intelligent Allocation Pruning & Leadership Preservation ───────────

def test_allocation_pruning_on_archival_and_restore(db_session, seed_users_and_employees):
    """
    Verify:
    1. Archiving a project deactivates workers while preserving PM and Team Lead.
    2. Restoring an archived project reactivates deallocated workers.
    """
    pm_emp = seed_users_and_employees["pm"]["employee"]
    tl_emp = seed_users_and_employees["tl"]["employee"]
    worker_emp = seed_users_and_employees["worker"]["employee"]
    admin_user = seed_users_and_employees["admin"]["user"]

    # Create active project with PM assigned
    project = make_project(
        name="Mission Critical Project",
        project_status="in progress",
        assigned_employee_ids=[pm_emp.id],
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    # 1. PM Allocation (Protected)
    pm_alloc = Allocation(
        employee_id=pm_emp.id,
        sub_project_id=project.id,
        is_active=True,
        total_daily_hours=8,
    )
    # 2. Team Lead Allocation (Protected by role_tags)
    tl_alloc = Allocation(
        employee_id=tl_emp.id,
        sub_project_id=project.id,
        is_active=True,
        role_tags=[TEAM_LEAD_ROLE_TAG],
        total_daily_hours=8,
    )
    # 3. Worker Allocation (Should be deallocated on archival)
    worker_alloc = Allocation(
        employee_id=worker_emp.id,
        sub_project_id=project.id,
        is_active=True,
        role_tags=["Annotator"],
        total_daily_hours=8,
    )

    db_session.add_all([pm_alloc, tl_alloc, worker_alloc])
    db_session.commit()

    mock_request = DummyRequest()
    mock_bg_tasks = MagicMock()

    # ── Action: Archive the project to 'completed' ──
    update_project(
        project_id=project.id,
        payload=ProjectUpdate(project_status="completed"),
        http_request=mock_request,
        background_tasks=mock_bg_tasks,
        db=db_session,
        current_user=admin_user,
    )

    db_session.refresh(pm_alloc)
    db_session.refresh(tl_alloc)
    db_session.refresh(worker_alloc)

    # Verify PM & Team Lead attribution PRESERVED
    assert pm_alloc.is_active is True, "PM allocation must remain active for leadership attribution"
    assert tl_alloc.is_active is True, "Team Lead allocation must remain active for attribution"

    # Verify Worker is deallocated and hours released
    assert worker_alloc.is_active is False, "Worker allocation must be deactivated on project archival"
    assert worker_alloc.deactivated_reason == "project_completed"
    assert worker_alloc.deactivated_at is not None

    # ── Action: Restore the project back to 'in progress' ──
    update_project(
        project_id=project.id,
        payload=ProjectUpdate(project_status="in progress"),
        http_request=mock_request,
        background_tasks=mock_bg_tasks,
        db=db_session,
        current_user=admin_user,
    )

    db_session.refresh(worker_alloc)
    # Verify Worker is reactivated
    assert worker_alloc.is_active is True, "Worker allocation must be reactivated on restore"
    assert worker_alloc.deactivated_reason is None
    assert worker_alloc.deactivated_at is None


# ── 5. Hard Delete Deprecation Guardrail ───────────────────────────────────

def test_delete_project_deprecation_guardrail(db_session, seed_users_and_employees):
    """
    Verify:
    1. Project with allocations cannot be hard deleted (HTTP 400).
    2. Empty draft project with 0 allocations can be cleanly deleted.
    """
    admin_user = seed_users_and_employees["admin"]["user"]
    worker_emp = seed_users_and_employees["worker"]["employee"]
    mock_request = DummyRequest()

    # Project A: Has an allocation
    project_with_alloc = make_project(
        name="Project With Allocations",
        project_status="active",
    )
    # Project B: Draft with 0 allocations
    project_empty = make_project(
        name="Empty Draft Project",
        project_status="active",
    )
    db_session.add_all([project_with_alloc, project_empty])
    db_session.commit()

    alloc = Allocation(
        employee_id=worker_emp.id,
        sub_project_id=project_with_alloc.id,
        is_active=True,
    )
    db_session.add(alloc)
    db_session.commit()

    # 1. Attempting to delete Project A must raise HTTP 400
    with pytest.raises(HTTPException) as exc_info:
        delete_project(
            project_id=project_with_alloc.id,
            http_request=mock_request,
            db=db_session,
            current_user=admin_user,
        )
    assert exc_info.value.status_code == 400
    assert "historical allocations" in exc_info.value.detail

    # Project A still exists
    assert db_session.query(Project).filter(Project.id == project_with_alloc.id).first() is not None

    # 2. Deleting Project B (0 allocations) succeeds
    res = delete_project(
        project_id=project_empty.id,
        http_request=mock_request,
        db=db_session,
        current_user=admin_user,
    )
    assert res == {"message": "Project deleted successfully"}
    # Project B is deleted
    assert db_session.query(Project).filter(Project.id == project_empty.id).first() is None
