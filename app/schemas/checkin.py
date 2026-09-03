
from pydantic import BaseModel, Field, validator
from datetime import date, datetime
from typing import List, Optional, Union

WORK_MODE_CHOICES = ["WFO", "WFH"]
MOOD_CHOICES = ["great", "okay", "low", "stressed"]


class CheckInCreate(BaseModel):
    work_mode: str
    project_ids: List[Union[int, str]] = Field(default_factory=list)
    mood: Optional[str] = None

    @validator("work_mode")
    def validate_work_mode(cls, v):
        if v not in WORK_MODE_CHOICES:
            raise ValueError(f"work_mode must be one of: {', '.join(WORK_MODE_CHOICES)}")
        return v

    @validator("project_ids")
    def validate_project_ids(cls, v):
        if not v:
            raise ValueError("Select at least one project you're working on today.")
        return v

    @validator("mood")
    def validate_mood(cls, v):
        if v is not None and v not in MOOD_CHOICES:
            raise ValueError(f"mood must be one of: {', '.join(MOOD_CHOICES)}")
        return v


class CheckOutUpdate(BaseModel):
    mood: Optional[str] = None

    @validator("mood")
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
    checked_in_at: Optional[datetime] = None
    checked_out_at: Optional[datetime] = None
    pm_confirmed_at: Optional[datetime] = None
    is_officially_allocated: bool = True


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
    kpi_confirmed: int = 0
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
