from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, model_validator


class AssignmentCreate(BaseModel):
    cluster_id: int
    assessment_id: int
    deadline: Optional[datetime] = None
    relative_due_days: Optional[int] = None

    @model_validator(mode="after")
    def validate_targets_and_schedule(self):
        if not self.assessment_id or self.assessment_id <= 0:
            raise ValueError("Phase 1 assignments require a valid assessment_id.")

        # Schedule check
        has_deadline = self.deadline is not None
        has_relative = self.relative_due_days is not None
        if not (has_deadline ^ has_relative):
            raise ValueError("Must specify exactly one of fixed 'deadline' or rolling 'relative_due_days'.")
        if has_relative and self.relative_due_days <= 0:
            raise ValueError("relative_due_days must be a positive integer.")

        return self


class AssignmentDeadlineUpdate(BaseModel):
    new_deadline: datetime


class AssignmentResponse(BaseModel):
    id: int
    cluster_id: int
    cluster_name: Optional[str] = None
    assessment_id: int
    assessment_title: Optional[str] = None
    deadline: Optional[datetime] = None
    relative_due_days: Optional[int] = None
    is_active: bool
    total_enrolled: int = 0
    completed_count: int = 0
    passed_count: int = 0
    in_progress_count: int = 0
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
