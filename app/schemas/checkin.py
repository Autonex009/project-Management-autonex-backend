from pydantic import BaseModel, Field, field_validator
from datetime import date, datetime
from typing import List, Optional, Union

WORK_MODE_CHOICES = ["WFO", "WFH"]
MOOD_CHOICES = ["great", "okay", "low", "stressed"]
OFFICE_FLOOR_CHOICES = ["7", "9", "17"]
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
    kpi_floor_17: int = 0
    kpi_order_tiffin: int = 0
    kpi_canteen: int = 0
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