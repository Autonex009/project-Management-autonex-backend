from datetime import date, datetime
from typing import List, Optional, Literal, Any
from pydantic import BaseModel, Field, field_validator, model_validator

from app.utils.business_time import today_ist

# Designation options
DesignationType = Literal["Program Manager", "Annotator", "Developer", "QA", "Reviewer"]
VALID_EMPLOYEE_TYPES = {"Full-time", "Part-time", "Intern", "Contract", "Contractor"}
EMPLOYEE_TYPE_ALIASES = {
    "Full-Time": "Full-time",
    "Full Time": "Full-time",
    "Part-Time": "Part-time",
    "Part Time": "Part-time",
    "Contract Based": "Contract",
    "Contract based": "Contract",
    "Contractor": "Contractor",
}


def normalize_encord_id(value: Optional[str]) -> Optional[str]:
    """Strip an Encord id before it is stored, and treat blank as unlinked.

    Analytics matches this against `encord_daily_time_spent.user_email`. Values
    imported from the onboarding spreadsheet arrived padded with whitespace, and a
    padded id silently resolves to no employee — the charts then label that person
    with their raw Encord email instead of their name. Normalizing on the way in
    keeps new edits and re-imports from reintroducing it.

    Removes ALL whitespace, not just the edges: an id is an email address, so inner
    whitespace is always a paste artefact, and this has to agree exactly with
    api\analytics.py's `_norm_encord` — which cannot rely on SQL ``trim`` (spaces
    only) and so strips the lot.
    """
    if value is None:
        return None
    cleaned = "".join(value.split())
    return cleaned or None


def normalize_employee_type(value: Optional[str]) -> Optional[str]:
    if value is None:
        return value
    normalized = EMPLOYEE_TYPE_ALIASES.get(value, value)
    if normalized not in VALID_EMPLOYEE_TYPES:
        raise ValueError("Invalid employee type")
    return normalized


def parse_and_validate_doj(value: Any) -> Optional[date]:
    """Parse string/datetime into date and assert not in future (Asia/Kolkata IST)."""
    if value is None or str(value).strip().lower() in ("", "null", "none"):
        return None
    parsed: Optional[date] = None
    if isinstance(value, str):
        val_str = value.split("T")[0].strip()
        try:
            parsed = date.fromisoformat(val_str)
        except ValueError:
            raise ValueError("Invalid date format for date_of_joining. Expected YYYY-MM-DD.")
    elif isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        raise ValueError("Invalid date type for date_of_joining.")

    if parsed and parsed > today_ist():
        raise ValueError("Date of joining cannot be in the future (IST).")
    return parsed


class EmployeeBase(BaseModel):
    name: str = Field(..., min_length=2)
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    razorpay_email: Optional[str] = Field(None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: Optional[str] = None
    slack_user_id: Optional[str] = None
    # Encord account email/identity; used to map Encord analytics to this employee.
    encord_id: Optional[str] = None
    avatar_url: Optional[str] = None
    employee_type: str
    date_of_joining: Optional[date] = None
    converted_to_fulltime_at: Optional[datetime] = None
    
    # Designation for role-based filtering
    designation: Optional[str] = "Annotator"
    work_model: Optional[str] = "WFO"
    
    working_hours_per_day: float = Field(8.0, gt=0, le=24)
    weekly_availability: float = Field(40.0, gt=0, le=168)
    
    skills: Optional[List[str]] = []
    productivity_baseline: float = Field(1.0, gt=0, le=2.0)
    status: Optional[str] = "active"

    @field_validator("razorpay_email", mode="before")
    @classmethod
    def validate_razorpay_email(cls, value: Optional[str]) -> Optional[str]:
        if not value or not str(value).strip():
            return None
        return str(value).strip().lower()

    @field_validator("employee_type", mode="before")
    @classmethod
    def validate_employee_type(cls, value: str) -> str:
        return normalize_employee_type(value)

    @field_validator("encord_id", mode="before")
    @classmethod
    def validate_encord_id(cls, value: Optional[str]) -> Optional[str]:
        return normalize_encord_id(value)

    @field_validator("date_of_joining", mode="before")
    @classmethod
    def validate_date_of_joining(cls, value):
        return parse_and_validate_doj(value)

    @field_validator("converted_to_fulltime_at", mode="before")
    @classmethod
    def validate_converted_to_fulltime_at(cls, value):
        if not value:
            return None
        if isinstance(value, str):
            val_str = value.strip()
            if not val_str:
                return None
            try:
                dt = datetime.fromisoformat(val_str)
                if dt.tzinfo is not None:
                    from datetime import timezone
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                return dt
            except Exception:
                pass
        return value

    @model_validator(mode="after")
    def validate_chronology(self):
        if self.date_of_joining and self.converted_to_fulltime_at:
            conv_date = (
                self.converted_to_fulltime_at.date()
                if isinstance(self.converted_to_fulltime_at, datetime)
                else self.converted_to_fulltime_at
            )
            if conv_date < self.date_of_joining:
                raise ValueError("Full-time start date cannot precede date of joining.")
        return self

    @field_validator("slack_user_id", mode="before")
    @classmethod
    def validate_slack_user_id(cls, value):
        if not value or not str(value).strip():
            return None
        return str(value).strip()


class EmployeeCreate(EmployeeBase):
    # Write-only: accepted on input, encrypted at rest, and never echoed back
    # (EmployeeResponse deliberately omits it — salary is only readable via the
    # admin payroll endpoints).
    base_salary: Optional[float] = None


class EmployeeUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = Field(None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    razorpay_email: Optional[str] = Field(None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: Optional[str] = None
    slack_user_id: Optional[str] = None
    encord_id: Optional[str] = None
    avatar_url: Optional[str] = None
    employee_type: Optional[str] = None
    date_of_joining: Optional[date] = None
    converted_to_fulltime_at: Optional[datetime] = None
    designation: Optional[str] = None
    work_model: Optional[str] = None
    
    working_hours_per_day: Optional[float] = None
    weekly_availability: Optional[float] = None
    
    skills: Optional[List[str]] = None
    productivity_baseline: Optional[float] = None
    status: Optional[str] = None
    base_salary: Optional[float] = None

    @field_validator("razorpay_email", mode="before")
    @classmethod
    def validate_razorpay_email(cls, value: Optional[str]) -> Optional[str]:
        if not value or not str(value).strip():
            return None
        return str(value).strip().lower()

    @field_validator("employee_type", mode="before")
    @classmethod
    def validate_employee_type(cls, value: Optional[str]) -> Optional[str]:
        return normalize_employee_type(value)

    @field_validator("encord_id", mode="before")
    @classmethod
    def validate_encord_id(cls, value: Optional[str]) -> Optional[str]:
        return normalize_encord_id(value)

    @field_validator("date_of_joining", mode="before")
    @classmethod
    def validate_date_of_joining(cls, value):
        return parse_and_validate_doj(value)

    @field_validator("converted_to_fulltime_at", mode="before")
    @classmethod
    def validate_converted_to_fulltime_at(cls, value):
        if not value:
            return None
        if isinstance(value, str):
            val_str = value.strip()
            if not val_str:
                return None
            try:
                dt = datetime.fromisoformat(val_str)
                if dt.tzinfo is not None:
                    from datetime import timezone
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                return dt
            except Exception:
                pass
        return value

    @model_validator(mode="after")
    def validate_chronology(self):
        if self.date_of_joining and self.converted_to_fulltime_at:
            conv_date = (
                self.converted_to_fulltime_at.date()
                if isinstance(self.converted_to_fulltime_at, datetime)
                else self.converted_to_fulltime_at
            )
            if conv_date < self.date_of_joining:
                raise ValueError("Full-time start date cannot precede date of joining.")
        return self

    @field_validator("slack_user_id", mode="before")
    @classmethod
    def validate_slack_user_id(cls, value):
        if not value or not str(value).strip():
            return None
        return str(value).strip()


class EmployeeResponse(EmployeeBase):
    id: int
    previous_employee_type: Optional[str] = None
    converted_to_fulltime_at: Optional[datetime] = None
    converted_by: Optional[int] = None
    assigned_projects: Optional[List[str]] = Field(default_factory=list)
    managers: Optional[List[str]] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EmployeeCreateResponse(EmployeeResponse):
    temp_password: Optional[str] = None
    portal_url: Optional[str] = None

