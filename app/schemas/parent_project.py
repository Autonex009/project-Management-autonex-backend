from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import date, datetime


class ParentProjectBase(BaseModel):
    """Base schema with common fields."""
    name: str = Field(..., min_length=2, description="Parent project name")
    program_manager_id: Optional[int] = Field(None, description="Employee ID of primary Program Manager")
    program_manager_ids: Optional[List[int]] = Field(None, description="Employee IDs of all Program Managers")
    description: Optional[str] = Field(None, description="Scope of work")
    client: Optional[str] = Field(None, description="Organization name")
    project_type: str = Field("Full", description="Project type: Full, POC, Side")
    global_start_date: Optional[date] = Field(None, description="Project start date")
    tentative_duration_months: Optional[int] = Field(None, ge=1, description="Expected duration in months")
    status: Optional[str] = Field("active", description="Status: active, completed, archived")
    is_annotation: Optional[bool] = Field(False, description="Flag indicating if this is an annotation project")


class ParentProjectCreate(ParentProjectBase):
    """Schema for creating a new organization. Only `name` is required; a PM is
    optional (attached automatically when a PM creates the organization)."""
    pass


class ParentProjectUpdate(BaseModel):
    """Schema for updating a parent project - all fields optional."""
    name: Optional[str] = Field(None, min_length=2)
    program_manager_id: Optional[int] = None
    program_manager_ids: Optional[List[int]] = None
    description: Optional[str] = None
    client: Optional[str] = None
    project_type: Optional[str] = None
    global_start_date: Optional[date] = None
    tentative_duration_months: Optional[int] = Field(None, ge=1)
    status: Optional[str] = None
    is_annotation: Optional[bool] = None



class SubProjectSummary(BaseModel):
    """Lightweight sub-project info for parent project responses."""
    id: int
    name: str
    batch_name: Optional[str] = None
    project_status: str
    
    class Config:
        from_attributes = True


class ParentProjectResponse(ParentProjectBase):
    """Response schema with all fields including computed."""
    id: int
    created_at: datetime
    updated_at: datetime
    sub_projects_count: int = 0
    program_manager_name: Optional[str] = None  # Joined from Employee table (primary PM)
    program_manager_names: List[str] = []  # Names of all PMs
    
    class Config:
        from_attributes = True


class ParentProjectWithSubProjects(ParentProjectResponse):
    """Extended response including sub-project list."""
    sub_projects: List[SubProjectSummary] = []
