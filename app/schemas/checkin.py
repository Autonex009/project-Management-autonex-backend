from pydantic import BaseModel, Field, field_validator
from datetime import date, datetime
from typing import List, Optional, Union

WORK_MODE_CHOICES = ["WFO", "WFH"]
MOOD_CHOICES = ["great", "okay", "low", "stressed"]
OFFICE_FLOOR_CHOICES = ["7", "9", "15", "17"]
LUNCH_PREFERENCE_CHOICES = ["order_tiffin", "canteen", "none"]
TIFFIN_TYPE_CHOICES = ["full_meal", "no_rice", "dal_and_rice"]


class CheckInCreate(BaseModel):
    work_mode: str
    project_ids: List[Union[int, str]] = Field(default_factory=list)
    mood: Optional[str] = None
    office_floor: Optional[str] = None  # Required if work_mode is "WFO"
    lunch_preference: Optional[str] = None  # Required if work_mode is "WFO"
    tiffin_type: Optional[str] = None  # Required if lunch_preference is "order_tiffin"

    @field_validator("work_mode")
    @classmethod
    def validate_work_mode(cls, v):
        if v not in WORK_MODE_CHOICES:
            raise ValueError(f"work_mode must be one of: {', '.join(WORK_MODE_CHOICES)}")
        return v

    @field_validator("project_ids")
    @classmethod
    def validate_project_ids(cls, v):
        if not v:
            raise ValueError("Select at least one project you're working on today.")
        return v

    @field_validator("mood")
    @classmethod
    def validate_mood(cls, v):
        if v is not None and v not in MOOD_CHOICES:
            raise ValueError(f"mood must be one of: {', '.join(MOOD_CHOICES)}")
        return v

    @field_validator("office_floor")
    @classmethod
    def validate_office_floor(cls, v, info):
        if info.data.get("work_mode") == "WFO":
            if not v:
                raise ValueError("Please select your office floor.")
            if v not in OFFICE_FLOOR_CHOICES:
                raise ValueError(f"office_floor must be one of: {', '.join(OFFICE_FLOOR_CHOICES)}")
        return v

    @field_validator("lunch_preference")
    @classmethod
    def validate_lunch_preference(cls, v, info):
        if info.data.get("work_mode") == "WFO":
            if not v:
                raise ValueError("Please select your lunch preference.")
            if v not in LUNCH_PREFERENCE_CHOICES:
                raise ValueError(f"lunch_preference must be one of: {', '.join(LUNCH_PREFERENCE_CHOICES)}")
        return v

    @field_validator("tiffin_type")
    @classmethod
    def validate_tiffin_type(cls, v, info):
        if info.data.get("lunch_preference") == "order_tiffin":
            if not v:
                raise ValueError("Please select your tiffin preference.")
            if v not in TIFFIN_TYPE_CHOICES:
                raise ValueError(f"tiffin_type must be one of: {', '.join(TIFFIN_TYPE_CHOICES)}")
        return v


class CheckOutUpdate(BaseModel):
    mood: Optional[str] = None

    @field_validator("mood")
    @classmethod
    def validate_mood(cls, v):
        if v is not None and v not in MOOD_CHOICES:
            raise ValueError(f"mood must be one of: {', '.join(MOOD_CHOICES)}")
        return v


class CheckInResponse(BaseModel):
    id: int
    employee_id: int
    checkin_date: date
    work_mode: str
    project_ids: List[Union[int, str]] = []
    mood: Optional[str] = None
    office_floor: Optional[str] = None
    lunch_preference: Optional[str] = None
    tiffin_type: Optional[str] = None
    checked_in_at: Optional[datetime] = None
    checked_out_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TodayCheckInStatus(BaseModel):
    """Prefill + status data the check-in modal needs to render itself."""
    already_checked_in: bool
    checkin: Optional[CheckInResponse] = None
    project_options: List[dict] = []  # [{project_id, project_name}]
    suggested_work_mode: str = "WFO"  # "WFH" if an approved WFH request covers today
    is_weekend_or_holiday: bool = False

    class Config:
        from_attributes = True


class TeamCheckInRow(BaseModel):
    """One employee's today-status on a PM/lead's roster view."""
    employee_id: int
    name: str
    avatar_url: Optional[str] = None
    designation: Optional[str] = None
    project_names: List[str] = []
    checked_in: bool
    work_mode: Optional[str] = None
    mood: Optional[str] = None
    office_floor: Optional[str] = None
    lunch_preference: Optional[str] = None
    tiffin_type: Optional[str] = None
    checked_in_at: Optional[datetime] = None
    checked_out_at: Optional[datetime] = None
    pm_confirmed_at: Optional[datetime] = None
    is_officially_allocated: bool = True
    is_on_leave: bool = False


class TeamCheckInSummary(BaseModel):
    date: date
    total: int
    checked_in: int
    confirmed: int
    rows: List[TeamCheckInRow] = []


class PaginatedTeamCheckIns(BaseModel):
    total: int
    page: int
    limit: int
    kpi_total: int = 0
    kpi_checked_in: int = 0
    kpi_wfo: int = 0
    kpi_wfh: int = 0
    kpi_confirmed: int = 0
    kpi_late: int = 0
    kpi_checked_out: int = 0
    kpi_floor_7: int = 0
    kpi_floor_9: int = 0
    kpi_floor_15: int = 0
    kpi_floor_17: int = 0
    kpi_order_tiffin: int = 0
    kpi_canteen: int = 0
    kpi_lunch_none: int = 0
    kpi_mood_great: int = 0
    kpi_mood_okay: int = 0
    kpi_mood_low: int = 0
    kpi_mood_stressed: int = 0
    kpi_approved_leaves_count: int = 0
    kpi_pending_leaves_count: int = 0
    kpi_approved_leaves_names: list[str] = []
    kpi_pending_leaves_names: list[str] = []
    items: List[TeamCheckInRow] = []


class ConfirmResult(BaseModel):
    confirmed: int


class MatrixRow(BaseModel):
    employee_id: int
    name: str
    avatar_url: Optional[str] = None
    designation: Optional[str] = None
    checkins: dict  # {"1": {"time": "10:00", "mode": "WFO"}, ...}


class MatrixResponse(BaseModel):
    month_year: str
    days_in_month: int
    rows: List[MatrixRow] = []



# -------------------------------------------------------------
# Check-in Pulse Dashboard Models (Minor Feature 04)
# -------------------------------------------------------------

class LeaveBreakdown(BaseModel):
    total: int = 0
    approved: int = 0
    pending: int = 0
    approved_checked_in: int = 0
    approved_checked_in_names: List[str] = []


class AttendanceOverview(BaseModel):
    effective_percentage: float
    checked_in: int
    expected: int
    active: int
    on_leave: int
    yet_to_check_in: int
    leave_breakdown: Optional[LeaveBreakdown] = None


class FloorBreakdown(BaseModel):
    floor_7: int = 0
    floor_9: int = 0
    floor_15: int = 0
    floor_17: int = 0
    wfh: int = 0


class AverageDetail(BaseModel):
    time: str
    status: str
    count: Optional[int] = None
    delta_minutes: Optional[int] = None


class ModeAverages(BaseModel):
    target_time: str = "09:30"
    overall: AverageDetail
    wfo: AverageDetail
    wfh: AverageDetail


class MoodPulse(BaseModel):
    total: int = 0
    great: int = 0
    okay: int = 0
    low: int = 0
    stressed: int = 0
    tiffins: int = 0
    canteen: int = 0


class CheckInPulseOverviewResponse(BaseModel):
    date: str
    updated_at: str
    attention_badge_count: int = 0
    attendance: AttendanceOverview
    floor_breakdown: FloorBreakdown
    averages: ModeAverages
    mood_pulse: MoodPulse


class TimingEmployeeItem(BaseModel):
    id: int
    name: str
    initials: str
    designation: Optional[str] = None
    work_mode: str
    office_floor: Optional[str] = None
    time: str


class TimingBucketItem(BaseModel):
    id: str
    label: str
    total: int
    wfo: int
    wfh: int
    is_late: bool = False
    employees: List[TimingEmployeeItem] = []


class CheckInPulseTimingResponse(BaseModel):
    date: str
    buckets: List[TimingBucketItem]


class StreakEmployeeItem(BaseModel):
    id: int
    name: str
    initials: str
    designation: Optional[str] = None
    last_seen: Optional[str] = None
    days: int


class StreakGroup(BaseModel):
    count: int = 0
    items: List[StreakEmployeeItem] = []


class ConsecutiveMisses(BaseModel):
    streak_2_days: StreakGroup
    streak_3_days: StreakGroup
    streak_5_plus_days: StreakGroup
    unauthorized_wfh: Optional[StreakGroup] = None


class UnaccountedEmployeeItem(BaseModel):
    id: int
    name: str
    initials: str
    designation: Optional[str] = None
    pm_name: Optional[str] = None
    slack_user_id: Optional[str] = None
    has_pending_leave: bool = False
    is_nudged_today: bool = False


class NotCheckedInNoLeaveGroup(BaseModel):
    count: int = 0
    employees: List[UnaccountedEmployeeItem] = []


class HabitualLateItem(BaseModel):
    id: int
    name: str
    initials: str
    designation: Optional[str] = None
    late_count: int
    note: str = "pattern, not one-off"


class HabitualLateGroup(BaseModel):
    count: int = 0
    description: str = ">3x this month"
    employees: List[HabitualLateItem] = []


class CheckInPulseAttentionResponse(BaseModel):
    date: str
    consecutive_misses: ConsecutiveMisses
    not_checked_in_no_leave: NotCheckedInNoLeaveGroup
    habitual_late: HabitualLateGroup


class NudgeRequest(BaseModel):
    employee_id: Optional[int] = None
    all_unaccounted: bool = False


class NudgeResponse(BaseModel):
    success: bool
    nudged_count: int
    message: str
