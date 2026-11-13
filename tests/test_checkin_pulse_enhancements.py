from datetime import date, datetime
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
from app.models.employee import Employee
from app.models.daily_checkin import DailyCheckIn
from app.models.leave import Leave
from app.models.wfh import WFHRequest
from app.services.checkin_pulse_service import (
    calculate_pulse_overview,
    calculate_pulse_timing,
    calculate_pulse_attention,
    get_pulse_data_cached,
    invalidate_checkin_pulse_cache,
)
from app.schemas.checkin import (
    CheckInPulseOverviewResponse,
    CheckInPulseTimingResponse,
    CheckInPulseAttentionResponse,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Employee.__table__,
            DailyCheckIn.__table__,
            Leave.__table__,
            WFHRequest.__table__,
        ],
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        # Seed an active employee and today's checkin
        emp = Employee(
            id=1,
            name="John Doe",
            email="john@example.com",
            designation="Developer",
            status="active",
            employee_type="Full-time",
            skills=[],
        )
        session.add(emp)
        session.flush()

        checkin = DailyCheckIn(
            id=1,
            employee_id=emp.id,
            checkin_date=date.today(),
            checked_in_at=datetime.now(),
            work_mode="WFO",
            office_floor="floor_7",
        )
        session.add(checkin)
        session.commit()

        yield session
    finally:
        session.close()


def test_pulse_timing_has_employees(db):
    data = calculate_pulse_timing(db)
    validated = CheckInPulseTimingResponse(**data)
    assert len(validated.buckets) == 5
    # Verify that buckets can contain employee items
    total_emp_in_buckets = sum(len(b.employees) for b in validated.buckets)
    total_bucket_counts = sum(b.total for b in validated.buckets)
    assert total_emp_in_buckets == total_bucket_counts
    assert total_emp_in_buckets > 0
    for b in validated.buckets:
        for emp in b.employees:
            assert emp.id > 0
            assert emp.name is not None
            assert emp.initials is not None
            assert emp.time is not None
            assert emp.work_mode in ("WFO", "WFH")


def test_pulse_attention_habitual_late(db):
    data = calculate_pulse_attention(db)
    validated = CheckInPulseAttentionResponse(**data)
    assert validated.habitual_late.description == ">3x past 30 days"
    assert validated.habitual_late.count == len(validated.habitual_late.employees)
    for emp in validated.habitual_late.employees:
        assert emp.late_count >= 3
        assert emp.name is not None
        assert emp.initials is not None


def test_pulse_caching_and_invalidation(db):
    today = date.today()
    # Fetch overview and timing through cached pipeline
    cached_1 = get_pulse_data_cached(db, "overview", today, None)
    assert cached_1 is not None
    assert "attendance" in cached_1

    timing_cached = get_pulse_data_cached(db, "timing", today, None)
    assert timing_cached is not None
    assert "buckets" in timing_cached

    # Fetch attention through cached pipeline
    attention_cached = get_pulse_data_cached(db, "attention", today, None)
    assert attention_cached is not None

    # Verify decoupled invalidation (only overview & timing)
    invalidate_checkin_pulse_cache(today, tabs=["overview", "timing"])

    # Refetch overview
    cached_2 = get_pulse_data_cached(db, "overview", today, None)
    assert cached_2 is not None

    # Full invalidation
    invalidate_checkin_pulse_cache(today)


def test_pulse_overview_leave_breakdown_with_pending_checked_in(db):
    today = date.today()

    # Create additional employees: one with approved leave + checked in, one with pending leave + checked in
    emp_approved = Employee(
        id=10,
        name="Approved Employee",
        email="approved@example.com",
        designation="QA",
        status="active",
        employee_type="Full-time",
        skills=[],
    )
    emp_pending = Employee(
        id=11,
        name="Pending Employee",
        email="pending@example.com",
        designation="Designer",
        status="active",
        employee_type="Full-time",
        skills=[],
    )
    db.add_all([emp_approved, emp_pending])
    db.flush()

    # Leaves
    leave_app = Leave(
        id=101,
        employee_id=emp_approved.id,
        leave_type="casual",
        start_date=today,
        end_date=today,
        status="approved",
    )
    leave_pend = Leave(
        id=102,
        employee_id=emp_pending.id,
        leave_type="sick",
        start_date=today,
        end_date=today,
        status="pending",
    )
    db.add_all([leave_app, leave_pend])

    # Both check in
    chk_app = DailyCheckIn(
        id=101,
        employee_id=emp_approved.id,
        checkin_date=today,
        checked_in_at=datetime.now(),
        work_mode="WFO",
        office_floor="floor_7",
    )
    chk_pend = DailyCheckIn(
        id=102,
        employee_id=emp_pending.id,
        checkin_date=today,
        checked_in_at=datetime.now(),
        work_mode="WFH",
    )
    db.add_all([chk_app, chk_pend])
    db.commit()

    data = calculate_pulse_overview(db, today)
    validated = CheckInPulseOverviewResponse(**data)
    lb = validated.attendance.leave_breakdown
    assert lb is not None
    assert lb.total == 2
    assert lb.approved == 1
    assert lb.pending == 1
    assert lb.approved_checked_in == 1
    assert "Approved Employee" in lb.approved_checked_in_names
    assert lb.pending_checked_in == 1
    assert "Pending Employee" in lb.pending_checked_in_names


def test_in_memory_ttl_cache_per_item_ttl():
    import time
    from app.services.project_cache import InMemoryTTLCache

    cache = InMemoryTTLCache(ttl_seconds=5.0, max_size=10)
    cache.set("short_lived", "value1", ttl=0.1)
    cache.set("default_lived", "value2")

    assert cache.get("short_lived") == "value1"
    assert cache.get("default_lived") == "value2"

    time.sleep(0.15)
    assert cache.get("short_lived") is None
    assert cache.get("default_lived") == "value2"


def test_decoupled_pulse_invalidation_preserves_attention(db):
    from app.services.checkin_pulse_service import (
        _pulse_l1_cache,
        PULSE_REDIS_KEY_PREFIX,
        get_pulse_data_cached,
        invalidate_checkin_pulse_cache,
    )

    today = date.today()
    d_str = today.isoformat()

    # Populate all 3 tabs into cache
    get_pulse_data_cached(db, "overview", today)
    get_pulse_data_cached(db, "timing", today)
    get_pulse_data_cached(db, "attention", today)

    overview_key = f"{PULSE_REDIS_KEY_PREFIX}:overview:all:{d_str}"
    timing_key = f"{PULSE_REDIS_KEY_PREFIX}:timing:all:{d_str}"
    attention_key = f"{PULSE_REDIS_KEY_PREFIX}:attention:all:{d_str}"

    assert _pulse_l1_cache.get(overview_key) is not None
    assert _pulse_l1_cache.get(timing_key) is not None
    assert _pulse_l1_cache.get(attention_key) is not None

    # Simulate checkin: invalidate only overview and timing
    invalidate_checkin_pulse_cache(today, tabs=["overview", "timing"])

    # Overview and timing must be purged
    assert _pulse_l1_cache.get(overview_key) is None
    assert _pulse_l1_cache.get(timing_key) is None

    # Attention MUST STILL BE IN RAM!
    assert _pulse_l1_cache.get(attention_key) is not None

    # Full invalidation should now purge attention
    invalidate_checkin_pulse_cache(today)
    assert _pulse_l1_cache.get(attention_key) is None

