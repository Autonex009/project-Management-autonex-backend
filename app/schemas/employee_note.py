from datetime import datetime
from typing import Optional, Literal

from pydantic import BaseModel, Field


NoteType = Literal["complaint", "warning", "recognition"]
Severity = Literal["low", "medium", "high"]
NoteStatus = Literal["open", "resolved"]


class EmployeeNoteCreate(BaseModel):
    employee_id: int
    type: NoteType
    title: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    severity: Optional[Severity] = None


class EmployeeNoteUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1)
    content: Optional[str] = Field(None, min_length=1)
    severity: Optional[Severity] = None


class EmployeeNoteResolve(BaseModel):
    resolution_note: Optional[str] = None


class EmployeeNoteResponse(BaseModel):
    id: int
    employee_id: int
    employee_name: Optional[str] = None
    type: str
    title: str
    content: str
    severity: Optional[str] = None
    status: str
    issued_by: Optional[int] = None
    issued_by_name: Optional[str] = None
    issued_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[int] = None
    resolution_note: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True