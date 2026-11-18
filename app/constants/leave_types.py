from datetime import date, timedelta, datetime, timezone

LEAVE_TYPE_CHOICES = ("paid", "casual_sick", "floater", "half_day", "first_half", "second_half")

LEAVE_TYPE_LABELS = {
    "paid": "Paid Leave",
    "casual_sick": "Casual/Sick Leave",
    "floater": "Floater Leave",
    "half_day": "Half-Day Leave",
    "first_half": "First Half-Day Leave",
    "second_half": "Second Half-Day Leave",
}

# Legacy values are still accepted so existing records continue to sync safely.
LEGACY_LEAVE_TYPE_ALIASES = {
    "vacation": "paid",
    "casual": "casual_sick",
    "sick": "casual_sick",
    "personal": "floater",
    "emergency": "floater",
    "half_day": "paid",
    "first_half": "paid",
    "second_half": "paid",
}

RAZORPAY_LEAVE_TYPE_IDS = {
    "paid": 0,
    "casual_sick": 1,
    "floater": 2,
}


# ── Annual paid-leave entitlement (per employee, per calendar year) ─────────
# Razorpay exposes no leave-balance API, so the entitlement is defined here and
# the remaining balance is computed locally as: quota − working-days already used
# (from approved leaves). A leave consumes its own type's balance; once a type's
# balance for the year is exhausted, further days of that type are treated as
# UNPAID leave (salary is deducted per working day).
#
# casual_sick is a single combined type in this system; 6 = 6 days total
# (not 6 casual + 6 sick). Adjust these numbers to change policy — no code edits
# elsewhere are required.
ANNUAL_LEAVE_QUOTA = {
    "paid": 12,
    "casual_sick": 6,
    "floater": 2,
}


# ── Intern paid-leave entitlement ───────────────────────────────────
# Interns accrue paid leave MONTHLY, not annually: 1 paid working-day per
# calendar month (resets each month). Days beyond that become UNPAID. Other
# leave types (casual_sick, floater) follow the same annual quotas as employees.
INTERN_EMPLOYEE_TYPE = "Intern"
INTERN_MONTHLY_PAID_QUOTA = 1


def is_intern(employee_type: str | None) -> bool:
    """True if the employee_type denotes an intern (case/space-insensitive)."""
    return (employee_type or "").strip().lower() == "intern"


def is_contractor(employee_type: str | None) -> bool:
    """True if the employee_type denotes a contractor/contract employee (case/space-insensitive)."""
    val = (employee_type or "").strip().lower()
    return val in ("contractor", "contract")


def is_intern_or_contractor(employee_type: str | None) -> bool:
    """True if the employee_type denotes an intern or a contractor."""
    return is_intern(employee_type) or is_contractor(employee_type)


def normalize_leave_type(leave_type: str) -> str:
    normalized = (leave_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    return LEGACY_LEAVE_TYPE_ALIASES.get(normalized, normalized)


def get_leave_type_label(leave_type: str) -> str:
    raw = (leave_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in LEAVE_TYPE_LABELS:
        return LEAVE_TYPE_LABELS[raw]
    normalized = normalize_leave_type(leave_type)
    return LEAVE_TYPE_LABELS.get(normalized, normalized.replace("_", " ").title())


def compute_prorated_paid_leave_quota(
    target_year: int,
    employee_type: str | None,
    converted_to_fulltime_at: datetime | date | str | None = None,
    date_of_joining: date | datetime | str | None = None,
    created_at: datetime | date | str | None = None,
) -> int:
    """
    Computes authoritative annual paid leave quota for an employee in target_year.

    Entitlement Rules:
      1. Interns / contractors accrue monthly (1 leave / month), annual quota is 1.
      2. Full-time employees:
         • Priority 1: converted_to_fulltime_at (promoted interns)
         • Priority 2: date_of_joining (direct full-time hires)
         • Priority 3: created_at (backup from signup acceptance)
      3. Multi-year Safety:
         • start_year < target_year  -> 12 days (employed whole year)
         • start_year == target_year -> (12 - start_month + 1) days
         • start_year > target_year  -> 0 days (future employee)
    """
    if is_intern_or_contractor(employee_type):
        return INTERN_MONTHLY_PAID_QUOTA

    def _parse_to_ist_date(val: datetime | date | str | None) -> date | None:
        if val is None:
            return None
        if isinstance(val, str):
            clean = val.split("T")[0].strip()
            if not clean or clean.lower() in ("none", "null", ""):
                return None
            try:
                return date.fromisoformat(clean)
            except Exception:
                return None
        if isinstance(val, datetime):
            ist = timezone(timedelta(hours=5, minutes=30))
            if val.tzinfo is None:
                val = val.replace(tzinfo=timezone.utc)
            return val.astimezone(ist).date()
        if isinstance(val, date):
            return val
        return None

    effective_start = (
        _parse_to_ist_date(converted_to_fulltime_at)
        or _parse_to_ist_date(date_of_joining)
        or _parse_to_ist_date(created_at)
    )

    if not effective_start:
        return ANNUAL_LEAVE_QUOTA.get("paid", 12)

    if effective_start.year < target_year:
        return ANNUAL_LEAVE_QUOTA.get("paid", 12)
    elif effective_start.year == target_year:
        remaining_months = 12 - effective_start.month + 1
        return max(1, min(12, remaining_months))
    else:
        return 0


def get_leave_quota(
    leave_type: str,
    target_year: int | None = None,
    employee_type: str | None = None,
    converted_to_fulltime_at: datetime | date | str | None = None,
    date_of_joining: date | datetime | str | None = None,
    created_at: datetime | date | str | None = None,
    custom_paid_quota: int | None = None,
) -> int:
    """
    Unified authoritative source of truth for ALL leave quotas across the system.

    Entitlement Logic:
      1. Paid Leave:
         • If custom_paid_quota is explicitly provided, respect it.
         • If target_year is provided, computes dynamic prorated quota based on
           precedence: converted_to_fulltime_at > date_of_joining > created_at.
         • Otherwise, returns base annual quota (12).
      2. Casual/Sick Leave:
         • Interns / contractors get 0.
         • Full-time employees get 6.
      3. Floater Leave:
         • Standard annual quota (2).
      4. Unknown / Other:
         • 0 (always unpaid).
    """
    norm = normalize_leave_type(leave_type)

    if norm == "paid":
        if custom_paid_quota is not None:
            return custom_paid_quota
        if target_year is not None:
            return compute_prorated_paid_leave_quota(
                target_year=target_year,
                employee_type=employee_type,
                converted_to_fulltime_at=converted_to_fulltime_at,
                date_of_joining=date_of_joining,
                created_at=created_at,
            )
        return ANNUAL_LEAVE_QUOTA.get("paid", 12)

    if norm == "casual_sick" and is_intern_or_contractor(employee_type):
        return 0

    return ANNUAL_LEAVE_QUOTA.get(norm, 0)


# ── Approved floater holiday dates (2026) ───────────────────────────
# Employees may only apply Floater Leave on these specific dates.
FLOATER_DATES_2026: frozenset[date] = frozenset([
    date(2026, 1, 14),   # Pongal / Makar Sankranti
    date(2026, 1, 23),   # Vasant Panchami
    date(2026, 2, 15),   # Maha Shivratri
    date(2026, 2, 19),   # Shivaji Jayanti
    date(2026, 3, 19),   # Ugadi / Gudi Padwa
    date(2026, 3, 21),   # Ramzan Eid
    date(2026, 3, 31),   # Mahavir Jayanti
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Ambedkar Jayanti
    date(2026, 5, 27),   # Bakrid
    date(2026, 8, 15),   # Independence Day
    date(2026, 8, 26),   # Onam
    date(2026, 8, 28),   # Raksha Bandhan
    date(2026, 9, 4),    # Janmashtami
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 8),   # Diwali
    date(2026, 11, 11),  # Bhai Duj
    date(2026, 11, 24),  # Guru Nanak Jayanti
    date(2026, 12, 23),  # Hazarat Ali's Birthday
])

# Fixed public holidays (2026) — not leave, just informational
FIXED_HOLIDAYS_2026: frozenset[date] = frozenset([
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 4),    # Holi
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 6, 26),   # Muharram
    date(2026, 9, 14),   # Ganesh Chaturthi
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 11, 9),   # Govardhan Puja
    date(2026, 12, 25),  # Christmas
])

FLOATER_DATES_BY_YEAR: dict[int, frozenset[date]] = {
    2026: FLOATER_DATES_2026,
}

FIXED_HOLIDAYS_BY_YEAR: dict[int, frozenset[date]] = {
    2026: FIXED_HOLIDAYS_2026,
}


def get_floater_dates_for_year(year: int) -> frozenset[date]:
    return FLOATER_DATES_BY_YEAR.get(year, frozenset())


def is_valid_floater_date(d: date) -> bool:
    return d in get_floater_dates_for_year(d.year)


def get_fixed_holidays_for_year(year: int) -> frozenset[date]:
    return FIXED_HOLIDAYS_BY_YEAR.get(year, frozenset())


def is_fixed_holiday(d: date) -> bool:
    return d in get_fixed_holidays_for_year(d.year)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # 5 = Saturday, 6 = Sunday


def is_non_working_day(d: date) -> bool:
    # Payroll treats fixed public holidays as WORKING days (they are paid days, not
    # days off the salary divisor), so only weekends are non-working here. Leave-day
    # counting in app/api/leaves.py still excludes holidays separately.
    return is_weekend(d)


def get_working_days_count(start_date: date, end_date: date, is_half_day: bool = False) -> int | float:
    """Calculate the number of working days in [start_date, end_date], excluding weekends and fixed public holidays."""
    if not start_date or not end_date or end_date < start_date:
        return 0
    count = 0
    curr = start_date
    while curr <= end_date:
        if not is_weekend(curr) and not is_fixed_holiday(curr):
            count += 1
        curr += timedelta(days=1)
    if is_half_day:
        return 0.5 if count > 0 else 0
    return count
