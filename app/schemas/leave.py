from pydantic import BaseModel, Field, validator, root_validator
from datetime import date
from typing import Optional
from app.constants.leave_types import LEAVE_TYPE_CHOICES, normalize_leave_type

class LeaveBase(BaseModel):
    employee_id: int = Field(..., gt=0)
    is_half_day: Optional[bool] = False
    half_day_slot: Optional[str] = None
    start_date: date
    end_date: date
    leave_type: str
    reason: Optional[str] = None

    @root_validator(pre=True)
    def map_half_day_leave_type(cls, values):
        leave_type = values.get("leave_type")
        if leave_type in ("first_half", "second_half"):
            values["is_half_day"] = True
            values["half_day_slot"] = leave_type
            if "start_date" in values:
                values["end_date"] = values["start_date"]
        return values

    @validator("leave_type")
    def validate_leave_type(cls, v):
        normalized = normalize_leave_type(v)
        if normalized not in LEAVE_TYPE_CHOICES:
            raise ValueError(f"leave_type must be one of: {', '.join(LEAVE_TYPE_CHOICES)}")
        return normalized

    @validator("half_day_slot")
    def validate_half_day_slot(cls, v, values):
        if values.get("is_half_day"):
            if v not in ("first_half", "second_half"):
                raise ValueError("half_day_slot must be 'first_half' or 'second_half' when is_half_day is True")
        else:
            if v is not None:
                raise ValueError("half_day_slot must be None when is_half_day is False")
        return v

    @validator("end_date")
    def end_after_start(cls, v, values):
        if "start_date" in values:
            if values.get("is_half_day"):
                if v != values["start_date"]:
                    raise ValueError("end_date must be equal to start_date for half-day leaves")
            elif v < values["start_date"]:
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
