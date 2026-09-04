"""Business calendar helpers — Autonex operates on IST."""
from datetime import datetime, date, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    return datetime.now(timezone.utc).astimezone(IST)


def today_ist() -> date:
    """Calendar date in IST — use for leave/WFH/on-leave/allocation 'today' checks."""
    return now_ist().date()
def calculate_business_days(start_date: date, num_days: int) -> date:
    """
    Calculates a future date by adding `num_days` business days.
    Skips weekends (Saturday, Sunday) and any date present in FIXED_HOLIDAYS_2026.
    """
    from app.constants.leave_types import FIXED_HOLIDAYS_2026
    
    current_date = start_date
    days_added = 0
    
    while days_added < num_days:
        current_date += timedelta(days=1)
        # Skip weekends (5 = Saturday, 6 = Sunday)
        if current_date.weekday() >= 5:
            continue
        # Skip fixed holidays
        if current_date in FIXED_HOLIDAYS_2026:
            continue
            
        days_added += 1
        
    return current_date
