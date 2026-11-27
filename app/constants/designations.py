"""Shared designation & role mappings used across employee management and dynamic cohorts."""

DESIGNATION_ROLE_MAP = {
    "Admin": "admin",
    "HR": "hr",   # combined Admin + PM access
    "Program Manager": "pm",
    # Reads everything a PM reads, approves nothing — see services/project_scope.py.
    # Must stay in step with DESIGNATION_ACCESS in api/auth.py.
    "Team Lead": "team_lead",
    "Annotator/ Reviewer": "employee",
    "Annotator/Reviewer": "employee",
    "Annotator": "employee",
    "Reviewer": "employee",
    "Developer": "employee",
}

ANNOTATOR_REVIEWER_DESIGNATIONS = (
    "Annotator/ Reviewer",
    "Annotator/Reviewer",
    "Annotator",
    "Reviewer",
    "QA",
)
DEVELOPER_DESIGNATIONS = ("Developer",)
PM_DESIGNATIONS = ("Program Manager",)
TEAM_LEAD_DESIGNATIONS = ("Team Lead",)
