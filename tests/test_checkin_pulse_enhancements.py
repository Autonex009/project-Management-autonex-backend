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
    Base.metadata.create_all(bind=engine)
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
    # Fetch overview through cached pipeline
    cached_1 = get_pulse_data_cached(db, "overview", today, None)
    assert cached_1 is not None
    assert "attendance" in cached_1

    # Fetch timing through cached pipeline
    timing_cached = get_pulse_data_cached(db, "timing", today, None)
    assert timing_cached is not None
    assert "buckets" in timing_cached

    # Invalidate cache
    invalidate_checkin_pulse_cache(today)

    # Refetch
    cached_2 = get_pulse_data_cached(db, "overview", today, None)
    assert cached_2 is not None
