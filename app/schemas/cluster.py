from datetime import date, datetime
from typing import Optional, List, Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator


ClusterScope = Literal[
    "custom",
    "everyone",
    "full_time",
    "annotators_reviewers",
    "developers",
    "project_managers",
    "team_leads",
    "new_joiners",
]


def _normalize_scope_inputs(
    dynamic_scope: Optional[str],
    dynamic_scopes: Optional[List[str]],
    joined_on_or_after: Optional[date],
) -> tuple[str, Optional[List[str]], Optional[date]]:
    """Canonicalizes single/multi dynamic scopes cleanly while preserving CHECK constraint parity."""
    if dynamic_scopes is not None:
        cleaned = list(dict.fromkeys(str(s) for s in dynamic_scopes if s and s != "custom"))
        if not cleaned:
            return "custom", None, None
        if "everyone" in cleaned:
            cleaned = ["everyone"]
        primary = "new_joiners" if "new_joiners" in cleaned else cleaned[0]
        if "new_joiners" in cleaned and joined_on_or_after is None:
            raise ValueError("joined_on_or_after is required when 'new_joiners' is selected.")
        if "new_joiners" not in cleaned:
            joined_on_or_after = None
        return primary, cleaned, joined_on_or_after

    single = dynamic_scope or "custom"
    if single == "custom":
        return "custom", None, None
    if single == "new_joiners" and joined_on_or_after is None:
        raise ValueError("joined_on_or_after is required when dynamic_scope is 'new_joiners'.")
    if single != "new_joiners":
        joined_on_or_after = None
    return single, [single], joined_on_or_after


class ClusterCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    dynamic_scope: ClusterScope = "custom"
    dynamic_scopes: Optional[List[ClusterScope]] = None
    joined_on_or_after: Optional[date] = None
    employee_ids: Optional[List[int]] = None

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v: str) -> str:
        if isinstance(v, str):
            v = v.strip()
        return v

    @model_validator(mode="after")
    def validate_scopes_and_date(self):
        scope, scopes, join_dt = _normalize_scope_inputs(
            self.dynamic_scope,
            self.dynamic_scopes,
            self.joined_on_or_after,
        )
        self.dynamic_scope = scope
        self.dynamic_scopes = scopes
        self.joined_on_or_after = join_dt
        return self


class ClusterUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    dynamic_scope: Optional[ClusterScope] = None
    dynamic_scopes: Optional[List[ClusterScope]] = None
    joined_on_or_after: Optional[date] = None

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v: Optional[str]) -> Optional[str]:
        if isinstance(v, str):
            v = v.strip()
        return v


class ClusterRestore(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    restore_assignments: bool = False

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v: Optional[str]) -> Optional[str]:
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v


class ClusterMemberAdd(BaseModel):
    employee_ids: List[int] = Field(..., min_length=1)


class ClusterMemberResponse(BaseModel):
    employee_id: int
    name: str
    email: str
    designation: Optional[str] = None
    employee_type: Optional[str] = None
    status: Optional[str] = None
    added_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ClusterResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    is_dynamic: bool = False
    dynamic_scope: str = "custom"
    dynamic_scopes: List[str] = Field(default_factory=list)
    joined_on_or_after: Optional[date] = None
    is_archived: bool
    member_count: int = 0
    active_assignments_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("dynamic_scopes", mode="before")
    @classmethod
    def normalize_none_scopes(cls, v):
        if v is None:
            return []
        return [s for s in v if s and s != "custom"]

    model_config = ConfigDict(from_attributes=True)


class ClusterPickerEmployee(BaseModel):
    id: int
    name: str
    email: str
    designation: Optional[str] = None
    employee_type: Optional[str] = None
    status: Optional[str] = None
    skills: List[str] = Field(default_factory=list)
    is_idle: bool = False
    daily_sheet_ids: List[int] = Field(default_factory=list)


class ClusterPickerDailySheet(BaseModel):
    id: int
    name: str
    main_project_name: Optional[str] = None
    project_status: Optional[str] = None
    active_member_count: int = 0


class ClusterPickerOptionsResponse(BaseModel):
    employees: List[ClusterPickerEmployee]
    daily_sheets: List[ClusterPickerDailySheet]
    designations: List[str]
    skills: List[str]
    employee_types: List[str]
    idle_count: int = 0
