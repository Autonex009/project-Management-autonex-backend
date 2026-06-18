from pydantic import BaseModel, Field, validator
from datetime import date
from typing import Optional
from app.constants.leave_types import LEAVE_TYPE_CHOICES, normalize_leave_type

class LeaveBase(BaseModel):
    employee_id: int = Field(..., gt=0)
    start_date: date
    end_date: date
    leave_type: str
    reason: Optional[str] = None

    @validator("leave_type")
    def validate_leave_type(cls, v):
        normalized = normalize_leave_type(v)
        if normalized not in LEAVE_TYPE_CHOICES:
            raise ValueError(f"leave_type must be one of: {', '.join(LEAVE_TYPE_CHOICES)}")
        return normalized

    @validator("end_date")
    def end_after_start(cls, v, values):
        if "start_date" in values and v < values["start_date"]:
            raise ValueError("end_date must be >= start_date")
        return v


class LeaveCreate(LeaveBase):
    pass

class LeaveUpdate(LeaveBase):
    pass

class Leave(LeaveBase):
    leave_id: int
    status: Optional[str] = "pending"
    approved_by: Optional[int] = None
    razorpay_applied: Optional[bool] = False
    flagged: Optional[bool] = False
    approval_remark: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
