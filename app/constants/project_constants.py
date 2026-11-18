"""
Project Constants & Lifecycle Status Taxonomy.

Single Source of Truth for:
- Active vs Archived project statuses
- Role tags for leadership preservation
- Predicates for tab taxonomy & allocation state machine
"""
from typing import Any

# Canonical active statuses (POC is actively worked by annotators / team leads)
ACTIVE_STATUSES: frozenset[str] = frozenset({
    "active",
    "in progress",
    "in-progress",
    "poc",
})

# Canonical archived/terminal/suspended statuses
ARCHIVED_STATUSES: frozenset[str] = frozenset({
    "completed",
    "on-hold",
    "on_hold",
    "cancelled",
    "archived",
    "poc_rejected",
    "poc_completed",
})

# Role tag for Team Leads used in allocations and project leadership attribution
TEAM_LEAD_ROLE_TAG: str = "Team Lead"


def normalize_project_status(status: str | None) -> str:
    """Normalize status string to lowercase stripped format, defaulting to 'active'."""
    if not status:
        return "active"
    return str(status).lower().strip()


def is_project_archived(status: str | None) -> bool:
    """
    Check if a project status belongs to the Archived bucket.
    Includes completed, on-hold, cancelled, and archived.
    """
    return normalize_project_status(status) in ARCHIVED_STATUSES


def is_project_active(status: str | None) -> bool:
    """
    Check if a project status belongs to the Active bucket.
    Includes in-progress, active, and POC.
    """
    return normalize_project_status(status) in ACTIVE_STATUSES


def is_project_development(project_types: Any) -> bool:
    """
    Determine if a project is classified as Development.
    Handles dictionary {'Development': ...}, list ['Development'], set, or None safely.
    """
    if not project_types:
        return False
    if isinstance(project_types, dict):
        return "Development" in project_types
    if isinstance(project_types, (list, set, tuple)):
        return "Development" in project_types
    if isinstance(project_types, str):
        return "development" in project_types.lower()
    return False
