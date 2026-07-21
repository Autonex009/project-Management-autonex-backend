from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Literal
from datetime import datetime

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


def normalize_employee_type(value: Optional[str]) -> Optional[str]:
    if value is None:
        return value
    normalized = EMPLOYEE_TYPE_ALIASES.get(value, value)
    if normalized not in VALID_EMPLOYEE_TYPES:
        raise ValueError("Invalid employee type")
    return normalized


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
    
    # Designation for role-based filtering
    designation: Optional[str] = "Annotator"
    
    working_hours_per_day: float = Field(8.0, gt=0, le=24)
    weekly_availability: float = Field(40.0, gt=0, le=168)
    
    skills: Optional[List[str]] = []
    productivity_baseline: float = Field(1.0, gt=0, le=2.0)
    status: Optional[str] = "active"

    @field_validator("employee_type", mode="before")
    @classmethod
    def validate_employee_type(cls, value: str) -> str:
        return normalize_employee_type(value)


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
    designation: Optional[str] = None
    
    working_hours_per_day: Optional[float] = None
    weekly_availability: Optional[float] = None
    
    skills: Optional[List[str]] = None
    productivity_baseline: Optional[float] = None
    status: Optional[str] = None
    base_salary: Optional[float] = None

    @field_validator("employee_type", mode="before")
    @classmethod
    def validate_employee_type(cls, value: Optional[str]) -> Optional[str]:
        return normalize_employee_type(value)


class EmployeeResponse(EmployeeBase):
    id: int
    previous_employee_type: Optional[str] = None
    converted_to_fulltime_at: Optional[datetime] = None
    converted_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
