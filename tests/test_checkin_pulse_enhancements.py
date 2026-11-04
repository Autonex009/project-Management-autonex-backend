from datetime import date
from app.db.database import SessionLocal
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

def test_pulse_timing_has_employees():
    db = SessionLocal()
    try:
        data = calculate_pulse_timing(db)
        validated = CheckInPulseTimingResponse(**data)
        assert len(validated.buckets) == 5
        # Verify that buckets can contain employee items
        total_emp_in_buckets = sum(len(b.employees) for b in validated.buckets)
        total_bucket_counts = sum(b.total for b in validated.buckets)
        assert total_emp_in_buckets == total_bucket_counts
        for b in validated.buckets:
            for emp in b.employees:
                assert emp.id > 0
                assert emp.name is not None
                assert emp.initials is not None
                assert emp.time is not None
                assert emp.work_mode in ("WFO", "WFH")
    finally:
        db.close()

def test_pulse_attention_habitual_late():
    db = SessionLocal()
    try:
        data = calculate_pulse_attention(db)
        validated = CheckInPulseAttentionResponse(**data)
        assert validated.habitual_late.description == ">3x past 30 days"
        assert validated.habitual_late.count == len(validated.habitual_late.employees)
        for emp in validated.habitual_late.employees:
            assert emp.late_count >= 3
            assert emp.name is not None
            assert emp.initials is not None
    finally:
        db.close()

def test_pulse_caching_and_invalidation():
    db = SessionLocal()
    try:
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
    finally:
        db.close()
