"""
Tests: Leave-Allocation Overlap Detection
==========================================
Policy: An employee on approved leave during a project period is STILL
eligible for allocation. Leave days are simply excluded from their working
availability in capacity calculations (handled by recommendation_service).
The check_leave_conflict() function detects overlaps and returns them as
informational warnings — it does NOT block the assignment.

What is tested (service layer — check_leave_conflict()):
  ● approved leave fully covering allocation → has_conflict=True (warning)
  ● approved leave partially overlapping (start) → has_conflict=True
  ● approved leave partially overlapping (end) → has_conflict=True
  ● single leave day inside allocation range → has_conflict=True
  ● leave date exactly equal to allocation start (boundary) → has_conflict=True
  ● leave date exactly equal to allocation end (boundary) → has_conflict=True
  ● allocation completely before leave → no conflict
  ● allocation completely after leave → no conflict
  ● pending leave overlapping → no conflict (only approved leaves matter)
  ● rejected leave overlapping → no conflict
  ● no leave at all → no conflict
  ● conflict result contains leave details for display
  ● no date range supplied → no conflict (check skipped)
"""

import pytest
from datetime import date, timedelta
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ── Bootstrap path so imports work when run from repo root ────────────────────
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.db.database import Base

# Import all models so SQLAlchemy can resolve cross-table FK references
# when building the in-memory SQLite schema.
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

from app.models.employee import Employee
from app.models.leave import Leave
from app.models.allocation import Allocation
from app.models.project import SubProject  # DailySheet alias
from app.services.allocation_validator import check_leave_conflict


# ── In-memory SQLite fixture ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture(scope="module")
def employee(db):
    emp = Employee(
        name="Test Employee",
        email="test.employee@example.com",
        employee_type="Full-time",
        designation="Annotator",
        working_hours_per_day=8.0,
        weekly_availability=40.0,
        status="active",
    )
    db.add(emp)
    db.commit()
    db.refresh(emp)
    return emp


# ── Helper ────────────────────────────────────────────────────────────────────

def make_leave(
    db,
    employee_id: int,
    start: date,
    end: date,
    status: str = "approved",
) -> Leave:
    leave = Leave(
        employee_id=employee_id,
        leave_type="paid",
        start_date=start,
        end_date=end,
        status=status,
    )
    db.add(leave)
    db.commit()
    db.refresh(leave)
    return leave


# ── Dates used across tests ───────────────────────────────────────────────────

TODAY = date.today()
LEAVE_START = TODAY + timedelta(days=10)
LEAVE_END   = TODAY + timedelta(days=14)   # 5-day leave


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — check_leave_conflict()
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckLeaveConflict:
    """
    Tests for check_leave_conflict() detection logic.
    has_conflict=True means the overlap was detected and should surface as a
    warning to the caller. The API layer no longer blocks on this — it allows
    the allocation and excludes leave days from capacity calculations.
    """

    def test_approved_leave_fully_covers_allocation(self, db, employee):
        """Allocation range sits entirely inside the leave period → conflict detected."""
        leave = make_leave(db, employee.id, LEAVE_START, LEAVE_END)
        alloc_start = LEAVE_START + timedelta(days=1)
        alloc_end   = LEAVE_START + timedelta(days=2)

        result = check_leave_conflict(db, employee.id, alloc_start, alloc_end)

        assert result["has_conflict"] is True, (
            f"Expected conflict for allocation {alloc_start}–{alloc_end} "
            f"within approved leave {LEAVE_START}–{LEAVE_END}"
        )
        db.delete(leave); db.commit()

    def test_approved_leave_partial_overlap_start(self, db, employee):
        """Allocation starts during leave → conflict."""
        leave = make_leave(db, employee.id, LEAVE_START, LEAVE_END)
        alloc_start = LEAVE_END - timedelta(days=1)   # overlaps last two days
        alloc_end   = LEAVE_END + timedelta(days=3)

        result = check_leave_conflict(db, employee.id, alloc_start, alloc_end)

        assert result["has_conflict"] is True
        db.delete(leave); db.commit()

    def test_approved_leave_partial_overlap_end(self, db, employee):
        """Allocation ends during leave → conflict."""
        leave = make_leave(db, employee.id, LEAVE_START, LEAVE_END)
        alloc_start = LEAVE_START - timedelta(days=3)  # starts before leave
        alloc_end   = LEAVE_START + timedelta(days=1)  # ends inside leave

        result = check_leave_conflict(db, employee.id, alloc_start, alloc_end)

        assert result["has_conflict"] is True
        db.delete(leave); db.commit()

    def test_single_leave_day_inside_allocation(self, db, employee):
        """Even a single-day leave inside a long allocation must block it."""
        single_day = TODAY + timedelta(days=20)
        leave = make_leave(db, employee.id, single_day, single_day)
        alloc_start = TODAY + timedelta(days=15)
        alloc_end   = TODAY + timedelta(days=25)

        result = check_leave_conflict(db, employee.id, alloc_start, alloc_end)

        assert result["has_conflict"] is True, (
            "A single leave day inside the allocation range must cause a conflict"
        )
        db.delete(leave); db.commit()

    def test_leave_on_allocation_start_date(self, db, employee):
        """Leave date equals allocation start → conflict (boundary inclusive)."""
        alloc_start = TODAY + timedelta(days=30)
        leave = make_leave(db, employee.id, alloc_start, alloc_start)

        result = check_leave_conflict(db, employee.id, alloc_start, alloc_start + timedelta(days=5))

        assert result["has_conflict"] is True
        db.delete(leave); db.commit()

    def test_leave_on_allocation_end_date(self, db, employee):
        """Leave date equals allocation end → conflict (boundary inclusive)."""
        alloc_end = TODAY + timedelta(days=40)
        leave = make_leave(db, employee.id, alloc_end, alloc_end)

        result = check_leave_conflict(db, employee.id, alloc_end - timedelta(days=5), alloc_end)

        assert result["has_conflict"] is True
        db.delete(leave); db.commit()

    def test_allocation_completely_before_leave(self, db, employee):
        """Allocation ends before leave starts → no conflict."""
        leave = make_leave(db, employee.id, LEAVE_START, LEAVE_END)
        alloc_end = LEAVE_START - timedelta(days=1)

        result = check_leave_conflict(db, employee.id, alloc_end - timedelta(days=5), alloc_end)

        assert result["has_conflict"] is False
        db.delete(leave); db.commit()

    def test_allocation_completely_after_leave(self, db, employee):
        """Allocation starts after leave ends → no conflict."""
        leave = make_leave(db, employee.id, LEAVE_START, LEAVE_END)
        alloc_start = LEAVE_END + timedelta(days=1)

        result = check_leave_conflict(db, employee.id, alloc_start, alloc_start + timedelta(days=5))

        assert result["has_conflict"] is False
        db.delete(leave); db.commit()

    def test_pending_leave_does_not_block(self, db, employee):
        """Pending leave must NOT block allocation (only approved leaves count)."""
        leave = make_leave(db, employee.id, LEAVE_START, LEAVE_END, status="pending")

        result = check_leave_conflict(db, employee.id, LEAVE_START, LEAVE_END)

        assert result["has_conflict"] is False, (
            "Pending leave should not block allocation assignment"
        )
        db.delete(leave); db.commit()

    def test_rejected_leave_does_not_block(self, db, employee):
        """Rejected leave must NOT block allocation."""
        leave = make_leave(db, employee.id, LEAVE_START, LEAVE_END, status="rejected")

        result = check_leave_conflict(db, employee.id, LEAVE_START, LEAVE_END)

        assert result["has_conflict"] is False, (
            "Rejected leave should not block allocation assignment"
        )
        db.delete(leave); db.commit()

    def test_no_leave_no_conflict(self, db, employee):
        """Employee with no leave at all → no conflict."""
        result = check_leave_conflict(
            db,
            employee.id,
            TODAY + timedelta(days=50),
            TODAY + timedelta(days=60),
        )
        assert result["has_conflict"] is False

    def test_conflict_result_contains_leave_details(self, db, employee):
        """Conflict result must include the offending leave for clear error messages."""
        leave = make_leave(db, employee.id, LEAVE_START, LEAVE_END)

        result = check_leave_conflict(db, employee.id, LEAVE_START, LEAVE_END)

        assert result["has_conflict"] is True
        assert "conflicting_leaves" in result
        assert len(result["conflicting_leaves"]) >= 1
        conflict = result["conflicting_leaves"][0]
        assert "leave_id" in conflict
        assert "start_date" in conflict
        assert "end_date" in conflict
        db.delete(leave); db.commit()

    def test_allocation_with_no_dates_skips_leave_check(self, db, employee):
        """
        If allocation has no active_start/end dates (open-ended),
        check_leave_conflict with None dates should return no conflict
        (caller decides how to handle open-ended allocations separately).
        """
        leave = make_leave(db, employee.id, LEAVE_START, LEAVE_END)

        result = check_leave_conflict(db, employee.id, None, None)

        # When no date range is supplied, cannot determine overlap → no conflict raised
        assert result["has_conflict"] is False
        db.delete(leave); db.commit()
