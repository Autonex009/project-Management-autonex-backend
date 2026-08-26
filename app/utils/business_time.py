"""Business calendar helpers — Autonex operates on IST."""
from datetime import datetime, date, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    return datetime.now(timezone.utc).astimezone(IST)


def today_ist() -> date:
    """Calendar date in IST — use for leave/WFH/on-leave/allocation 'today' checks."""
    return now_ist().date()