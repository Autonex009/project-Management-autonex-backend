import time
import pytest

# Ensure all SQLAlchemy models are registered
import app.models.allocation       # noqa: F401
import app.models.employee         # noqa: F401
import app.models.parent_project   # noqa: F401
import app.models.project          # noqa: F401
import app.models.sub_project      # noqa: F401
import app.models.user             # noqa: F401
import app.models.daily_checkin    # noqa: F401
import app.models.guideline        # noqa: F401

from app.api.projects import get_my_projects
from app.models.user import User
from app.models.employee import Employee
from app.schemas.project import MyProjectsResponse, MyProjectItem
from app.services.project_cache import my_projects_cache, invalidate_my_projects_cache
from app.db.database import SessionLocal


def test_my_projects_unlinked_user():
    """A user with no employee_id must return a clean empty payload without DB queries."""
    unlinked_user = User(id=99999, email="no_emp@example.com", role="employee", employee_id=None)
    resp = get_my_projects(current_user=unlinked_user)

    assert isinstance(resp, MyProjectsResponse)
    assert resp.employee_id == 0
    assert resp.total_projects == 0
    assert resp.permanent_count == 0
    assert resp.temporary_count == 0
    assert resp.permanent_projects == []
    assert resp.temporary_projects == []


def test_my_projects_ttl_cache_and_invalidation():
    """Verify in-memory TTL caching and event-driven invalidation."""
    test_emp_id = 999988
    invalidate_my_projects_cache(test_emp_id)

    dummy_payload = MyProjectsResponse(
        employee_id=test_emp_id,
        total_projects=1,
        permanent_count=1,
        temporary_count=0,
        checked_in_today_any=True,
        permanent_projects=[],
        temporary_projects=[],
    )

    # 1. Miss initially
    assert my_projects_cache.get(test_emp_id) is None

    # 2. Store in cache
    my_projects_cache.set(test_emp_id, dummy_payload)
    cached = my_projects_cache.get(test_emp_id)
    assert cached is not None
    assert cached.employee_id == test_emp_id
    assert cached.checked_in_today_any is True

    # 3. Invalidate specific employee
    invalidate_my_projects_cache(test_emp_id)
    assert my_projects_cache.get(test_emp_id) is None


def test_my_projects_db_fetch_and_hot_cache_performance():
    """Test cold DB fetch followed by sub-millisecond hot cache hit."""
    with SessionLocal() as db:
        emp = db.query(Employee).first()
        if not emp:
            pytest.skip("No employees in test DB")

        user = User(id=emp.id, email=emp.email, role="employee", employee_id=emp.id)

        # Clear cache first to guarantee cold fetch
        invalidate_my_projects_cache(emp.id)

        # Cold DB fetch
        t0 = time.time()
        cold_resp = get_my_projects(current_user=user)
        cold_time_ms = (time.time() - t0) * 1000

        assert isinstance(cold_resp, MyProjectsResponse)
        assert cold_resp.employee_id == emp.id

        # Hot Cache fetch
        t1 = time.time()
        hot_resp = get_my_projects(current_user=user)
        hot_time_ms = (time.time() - t1) * 1000

        assert hot_resp == cold_resp
        assert hot_time_ms < 5.0  # Hot cache must return within 5ms (typically < 0.1ms)
