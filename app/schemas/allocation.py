from pydantic import BaseModel, Field, field_validator
from datetime import date, datetime
from typing import Optional, List, Dict


class AllocationBase(BaseModel):
    """Base allocation fields."""
    employee_id: int = Field(..., gt=0)
    sub_project_id: int = Field(..., gt=0)
    total_daily_hours: int = Field(8, ge=1, le=12, description="Daily hours (1-12)")
    active_start_date: Optional[date] = Field(None, description="When employee starts on project")
    active_end_date: Optional[date] = Field(None, description="When employee ends on project")
    role_tags: Optional[List[str]] = Field(default=[], description="Role tags like 'Annotation', 'Review'")
    time_distribution: Optional[Dict[str, int]] = Field(default={}, description="Hours per role tag")
    override_flag: bool = Field(False, description="True if forced despite warnings")
    override_reason: Optional[str] = Field(None, description="Reason for override")
    productivity_override: Optional[float] = Field(1.0, gt=0)
    
    # Legacy fields for backward compatibility
    weekly_hours_allocated: Optional[float] = Field(None, gt=0)
    weekly_tasks_allocated: Optional[int] = Field(None, ge=0)
    effective_week: Optional[date] = None


class AllocationCreate(AllocationBase):
    """Schema for creating a new allocation."""
    
    @field_validator('time_distribution')
    @classmethod
    def validate_time_distribution(cls, v, info):
        """Validate that time distribution doesn't exceed total hours."""
        # Note: Full validation happens in the service layer
        if v:
            for tag, hours in v.items():
                if hours < 0:
                    raise ValueError(f"Hours for '{tag}' cannot be negative")
        return v


class AllocationUpdate(BaseModel):
    """Schema for updating an allocation - all fields optional."""
    employee_id: Optional[int] = Field(None, gt=0)
    sub_project_id: Optional[int] = Field(None, gt=0)
    total_daily_hours: Optional[int] = Field(None, ge=1, le=12)
    active_start_date: Optional[date] = None
    active_end_date: Optional[date] = None
    role_tags: Optional[List[str]] = None
    time_distribution: Optional[Dict[str, int]] = None
    override_flag: Optional[bool] = None
    override_reason: Optional[str] = None
    productivity_override: Optional[float] = Field(None, gt=0)
    weekly_hours_allocated: Optional[float] = Field(None, gt=0)
    weekly_tasks_allocated: Optional[int] = Field(None, ge=0)
    effective_week: Optional[date] = None


class AllocationResponse(AllocationBase):
    """Response schema with all fields."""
    id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    # Computed fields for UI
    employee_name: Optional[str] = None
    project_name: Optional[str] = None
    
    class Config:
        from_attributes = True


class AllocationValidationRequest(BaseModel):
    """Request schema for validation endpoint."""
    employee_id: int
    sub_project_id: int
    total_daily_hours: int = Field(..., ge=1, le=12)
    time_distribution: Optional[Dict[str, int]] = {}
    active_start_date: Optional[date] = None
    active_end_date: Optional[date] = None
    exclude_allocation_id: Optional[int] = None


class AllocationValidationResponse(BaseModel):
    """Response for validation endpoint."""
    is_valid: bool
    time_distribution_valid: bool
    double_booking_check: dict
    errors: List[str] = []
    warnings: List[str] = []


class EmployeeAllocationStatus(BaseModel):
    """Employee allocation status for UI grouping."""
    employee_id: int
    employee_name: str
    status: str  # 'unallocated', 'partial', 'full'
    total_allocated: int
    max_capacity: int
    utilization_percent: float

