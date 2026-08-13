from datetime import date, datetime
from typing import Optional, Any, List

from pydantic import BaseModel, Field


class BadgeCatalogItem(BaseModel):
    code: str
    name: str
    description: str
    category: str
    period_type: str
    expires: bool


class EmployeeBadgeCreate(BaseModel):
    employee_id: int
    badge_code: str
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    expires_at: Optional[datetime] = None
    meta: Optional[dict[str, Any]] = None


class EmployeeBadgeResponse(BaseModel):
    id: int
    employee_id: int
    employee_name: Optional[str] = None
    badge_code: str
    badge_name: Optional[str] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    expires_at: Optional[datetime] = None
    status: str
    awarded_at: Optional[datetime] = None
    awarded_by: Optional[int] = None
    meta: Optional[dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class EmployeeBadgeLogResponse(BaseModel):
    id: int
    employee_badge_id: Optional[int] = None
    employee_id: int
    employee_name: Optional[str] = None
    badge_code: str
    badge_name: Optional[str] = None
    action: str
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    details: Optional[dict[str, Any]] = None
    actor_id: Optional[int] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True