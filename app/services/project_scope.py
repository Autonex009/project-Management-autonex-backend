"""Per-project authorisation: *which* project a caller may act on, not just their role.

``require_role("admin", "pm")`` answers "is this caller a manager?" — it cannot answer "is
this caller a manager of *this* project?". Only the second question keeps one project's
staff out of another project's decisions, so every mutating endpoint asks it here.

**Acting on a project.** A team lead has the same powers as a program manager, so both
qualify, and both are scoped the same way — to the projects they actually run:

    may act on a project ⟺ that project's PM or team lead (or admin/HR)

**Acting on a person's own request** (leave, WFH, evaluation) is a separate question,
because the hierarchy only shows up here. Who may decide depends on the *subject's*
designation, not on the caller's:

    program manager / project manager / HR  →  admin only
    team lead                               →  their project's PM, or admin
    everyone else                           →  their project's PM or team lead, or admin

So a request always travels *up*: a lead decides for their members but not for a peer
lead, and a manager's own request leaves the project entirely. Decided by designation
rather than ``users.role`` or anything recorded per-project, so a person's rank is the same
on every project they touch and cannot be altered by how they were added to one.

Mirrors ``roleAccess.js`` and ``resolvePmIds`` on the frontend; the two must agree, or the
UI hides a control the API allows (or offers one it rejects).
"""
from __future__ import annotations

from typing import Iterable, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.allocation import Allocation
from app.models.employee import Employee
from app.models.parent_project import MainProject
from app.models.project import DailySheet
from app.models.user import User

# Roles that bypass project scoping entirely. HR carries combined Admin + PM access,
# matching require_role.
FULL_ACCESS_ROLES = ("admin", "hr")

# ── Subject tiers ───────────────────────────────────────────────────
# Who may decide a person's own request, keyed on that person's designation. Matched
# case-insensitively after trimming, and against every spelling in use: the column is free
# text with no constraint, "project manager" and "program manager" are used
# interchangeably, and designations imported from sheets carry stray whitespace. A variant
# that slipped through would fail *silently* — the request would quietly become decidable
# by someone a tier below.

# Managers. Their own requests leave the project: an admin decides, so nobody approves a
# peer (or themselves).
ADMIN_ONLY_SUBJECT_DESIGNATIONS = frozenset(
    {"program manager", "project manager", "hr"}
)

# Leads. Their own requests go up to the program manager of their project, never sideways
# to another lead on it.
PM_ONLY_SUBJECT_DESIGNATIONS = frozenset({"team lead"})

# Values marking an allocation as that project's lead. Mirrors isTeamLeadAllocation in
# frontend/src/utils/roleAccess.js. The tag is what the Team Lead picker writes, so it is
# the primary record; the designation is also accepted so allocations made before the tag
# existed, or from the Allocations page, still resolve.
TEAM_LEAD_TAGS = frozenset({"team lead"})


def _as_int_set(values: Optional[Iterable]) -> set[int]:
    """Coerce a JSON id list to a set of ints, discarding anything unusable.

    ``assigned_employee_ids`` and ``program_manager_ids`` are JSON columns with no
    foreign key, so they accumulate whatever was written: ints, numeric strings from a
    form post, and stale ids of deleted employees. Anything non-numeric is dropped
    rather than raising — a malformed entry must not 500 an approval.
    """
    if not values:
        return set()
    result: set[int] = set()
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _main_project_pm_ids(main_project: Optional[MainProject]) -> set[int]:
    """PMs recorded at the organisation level, newest column first."""
    if main_project is None:
        return set()
    ids = _as_int_set(getattr(main_project, "program_manager_ids", None))
    if ids:
        return ids
    # Legacy single-PM column, still populated on older rows.
    return _as_int_set([getattr(main_project, "program_manager_id", None)])


def _normalise(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _designations(db: Session, ids: set[int]) -> dict[int, str]:
    """employee id -> normalised designation, in one query."""
    if not ids:
        return {}
    return {
        employee.id: _normalise(employee.designation)
        for employee in db.query(Employee).filter(Employee.id.in_(ids)).all()
    }


def _assigned_manager_ids(db: Session, project: Optional[DailySheet]) -> set[int]:
    """Whoever occupies ``project``'s manager slot, before designation is considered.

    Project-level assignment wins; an organisation's PMs apply only to projects that name
    no PM of their own. That ordering is what lets several PMs share one client while each
    owns distinct projects under it.
    """
    if project is None:
        return set()
    ids = _as_int_set(getattr(project, "assigned_employee_ids", None))
    if ids:
        return ids
    main_project_id = getattr(project, "main_project_id", None)
    if not main_project_id:
        return set()
    main_project = db.query(MainProject).filter(MainProject.id == main_project_id).first()
    return _main_project_pm_ids(main_project)


def _demoted_to_lead(db: Session, project: Optional[DailySheet]) -> set[int]:
    """Manager-slot occupants whose designation now says Team Lead.

    People converted from Program Manager to Team Lead keep their seat in
    ``assigned_employee_ids`` until someone edits the project, so the stored data still
    calls them managers. Designation is the newer, deliberate statement of rank, so it
    wins: they are shown and treated as this project's lead without waiting for a manual
    cleanup pass over every project.
    """
    ids = _assigned_manager_ids(db, project)
    designations = _designations(db, ids)
    return {i for i in ids if designations.get(i) in TEAM_LEAD_TAGS}


def project_pm_ids(db: Session, project: Optional[DailySheet]) -> set[int]:
    """The ``employees.id`` set that manages ``project``.

    Excludes anyone the roster now designates a Team Lead — see :func:`_demoted_to_lead`.
    A project whose entire manager slot has been converted therefore resolves to no PM,
    which is accurate: it has leads and no manager, and a lead's own request there
    escalates to an admin.
    """
    ids = _assigned_manager_ids(db, project)
    if not ids:
        return set()
    return ids - _demoted_to_lead(db, project)


def project_lead_ids(db: Session, project: Optional[DailySheet]) -> set[int]:
    """The ``employees.id`` set leading ``project``.

    Leads are recorded as allocations, not as a column, because leadership is per-project:
    the same person leads one project without leading every project they are on. Qualifying
    by the tag or by the designation covers both what the picker writes and allocations made
    outside it.

    Also includes anyone sitting in the manager slot whose designation now says Team Lead,
    so a conversion takes effect immediately instead of waiting for the project to be
    edited. Nothing is *written* to ``assigned_employee_ids`` here — that column stays the
    manager slot, and a lead placed there deliberately would still be read as a lead.
    """
    if project is None:
        return set()
    rows = (
        db.query(Allocation.employee_id, Allocation.role_tags)
        .filter(Allocation.sub_project_id == project.id)
        .all()
    )
    # No allocations does not mean no leads: a converted manager still occupies the manager
    # slot without necessarily holding an allocation row.
    if not rows:
        return _demoted_to_lead(db, project)
    employee_ids = {row[0] for row in rows if row[0] is not None}
    designations = _designations(db, employee_ids)
    leads: set[int] = set()
    for employee_id, role_tags in rows:
        if employee_id is None:
            continue
        tagged = any(
            _normalise(tag) in TEAM_LEAD_TAGS for tag in (role_tags or [])
        )
        if tagged or designations.get(employee_id) in TEAM_LEAD_TAGS:
            leads.add(int(employee_id))
    return leads | _demoted_to_lead(db, project)


def project_actor_ids(db: Session, project: Optional[DailySheet]) -> set[int]:
    """Everyone who may act on ``project`` — its PM(s) and its lead(s)."""
    return project_pm_ids(db, project) | project_lead_ids(db, project)


def has_full_access(user: Optional[User]) -> bool:
    return user is not None and user.role in FULL_ACCESS_ROLES


def _actor_employee_id(user: Optional[User]) -> Optional[int]:
    """The caller's ``employees.id``.

    Deliberately not ``user.id``: that is a ``users`` primary key, while every PM id in
    the system is an ``employees`` primary key. The two sequences are unrelated, so
    comparing them silently matches the wrong person.
    """
    return getattr(user, "employee_id", None)


def is_project_pm(db: Session, user: Optional[User], project: Optional[DailySheet]) -> bool:
    """True when ``user`` is specifically ``project``'s program manager (or admin/HR).

    Stricter than :func:`can_act_on_project` — used where a lead is deliberately not
    enough, namely deciding a lead's own request.
    """
    if has_full_access(user):
        return True
    actor_employee_id = _actor_employee_id(user)
    if actor_employee_id is None:
        return False
    return actor_employee_id in project_pm_ids(db, project)


def can_act_on_project(
    db: Session, user: Optional[User], project: Optional[DailySheet]
) -> bool:
    """True when ``user`` may act on ``project`` — its PM or its lead, or admin/HR."""
    if has_full_access(user):
        return True
    actor_employee_id = _actor_employee_id(user)
    if actor_employee_id is None:
        return False
    return actor_employee_id in project_actor_ids(db, project)


def employee_project_ids(db: Session, employee_id: int) -> set[int]:
    """Daily-sheet ids the employee is allocated to."""
    rows = (
        db.query(Allocation.sub_project_id)
        .filter(Allocation.employee_id == employee_id)
        .all()
    )
    return {row[0] for row in rows if row[0] is not None}


def managed_projects_of_employee(
    db: Session,
    user: Optional[User],
    employee_id: Optional[int],
    *,
    pm_only: bool = False,
) -> set[int]:
    """Of the projects ``employee_id`` is on, those ``user`` may decide for.

    ``pm_only`` narrows the test to the project's program manager, which is what a *lead's*
    own request requires — a peer lead on the same project must not qualify.

    Filtered in Python rather than SQL because PM assignment lives in JSON columns and the
    containment operators differ between Postgres and SQLite. Only the employee's own
    projects are loaded, so this stays a handful of rows.
    """
    actor_employee_id = _actor_employee_id(user)
    if actor_employee_id is None or employee_id is None:
        return set()
    project_ids = employee_project_ids(db, employee_id)
    if not project_ids:
        return set()
    projects = db.query(DailySheet).filter(DailySheet.id.in_(project_ids)).all()
    resolve = project_pm_ids if pm_only else project_actor_ids
    return {
        project.id
        for project in projects
        if actor_employee_id in resolve(db, project)
    }


def _subject_designation(db: Session, employee_id: Optional[int]) -> str:
    if employee_id is None:
        return ""
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    return _normalise(employee.designation) if employee else ""


def escalates_to_admin(db: Session, employee_id: Optional[int]) -> bool:
    """True when only an admin may decide this person's own requests.

    Managers, so that nobody approves a peer manager — or themselves.
    """
    return _subject_designation(db, employee_id) in ADMIN_ONLY_SUBJECT_DESIGNATIONS


def escalates_to_pm(db: Session, employee_id: Optional[int]) -> bool:
    """True when this person's own requests need their project's PM (or an admin).

    Leads. Their requests travel up rather than sideways: another lead on the same project
    has the same rank, so letting one decide for the other would erase the hierarchy this
    role was introduced to create.
    """
    return _subject_designation(db, employee_id) in PM_ONLY_SUBJECT_DESIGNATIONS


def can_manage_employee(db: Session, user: Optional[User], employee_id: Optional[int]) -> bool:
    """True when ``user`` may act on requests belonging to ``employee_id``.

    Which tier applies depends on the *subject*, not the caller — see the module docstring.
    Managing one of the employee's projects is enough when several apply: someone allocated
    across two projects can be decided by either project's manager, and ``approved_by``
    plus the audit entry record who actually did it.
    """
    if has_full_access(user):
        return True
    if escalates_to_admin(db, employee_id):
        return False
    return bool(
        managed_projects_of_employee(
            db, user, employee_id, pm_only=escalates_to_pm(db, employee_id)
        )
    )


# ── Guards ──────────────────────────────────────────────────────────
# Each 403 names the tier that applied. "Not your project" and "this person outranks the
# project" call for different fixes from whoever reads the message.


def _forbid(action: str, scope: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"You can only {action} for {scope} you manage.",
    )


def require_project_scope(
    db: Session,
    user: Optional[User],
    project: Optional[DailySheet],
    *,
    action: str = "perform this action",
) -> None:
    """Allow the PM or a lead of ``project`` (or admin/HR)."""
    if can_act_on_project(db, user, project):
        return
    _forbid(action, "projects")


def require_employee_scope(
    db: Session,
    user: Optional[User],
    employee_id: Optional[int],
    *,
    action: str = "perform this action",
) -> None:
    """Allow whoever the subject's tier permits — see the module docstring."""
    if can_manage_employee(db, user, employee_id):
        return
    # These two read as wrong if reported as "projects you manage": the caller may well
    # manage this person's project and still be the wrong rank to decide.
    if escalates_to_admin(db, employee_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"This request belongs to a manager, so only an admin can {action} "
                f"for them."
            ),
        )
    if escalates_to_pm(db, employee_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"This request belongs to a team lead, so only their program manager "
                f"(or an admin) can {action} for them."
            ),
        )
    _forbid(action, "employees on projects")
