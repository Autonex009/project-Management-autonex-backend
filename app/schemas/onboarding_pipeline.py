"""
Pydantic schemas for the Onboarding Pipeline endpoints.
"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


# ── Requests ─────────────────────────────────────────────────────────

class PipelineAssignRequest(BaseModel):
    """PM/HR assigns a candidate to a project and buddy."""
    candidate_id: int
    project_id: int
    buddy_id: int
    escalation_days: Optional[int] = 5


class PipelineBulkAssignRequest(BaseModel):
    """PM/HR assigns multiple candidates to the same project and buddy."""
    candidate_ids: list[int]
    project_id: int
    buddy_id: int
    escalation_days: Optional[int] = 5


class PipelineEvaluateRequest(BaseModel):
    """Buddy/PM submits the Day-5 evaluation."""
    score: int = Field(..., ge=0, le=100, description="Evaluation score 0-100")
    notes: Optional[str] = None
    result: str = Field(..., pattern="^(passed|failed)$", description="Must be 'passed' or 'failed'")


# ── Responses ────────────────────────────────────────────────────────

class PipelineResponse(BaseModel):
    """Full pipeline record returned to the frontend."""
    id: int
    candidate_id: int
    candidate_name: Optional[str] = None
    candidate_email: Optional[str] = None
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    buddy_id: Optional[int] = None
    buddy_name: Optional[str] = None
    status: str
    days_elapsed: Optional[int] = None
    started_at: Optional[datetime] = None
    evaluated_at: Optional[datetime] = None
    eval_score: Optional[int] = None
    eval_notes: Optional[str] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True
