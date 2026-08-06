"""Per-project authorisation: who may act on what.

Two rules, and keeping them apart is the whole point:

* **Acting on a project** — its program manager *or* one of its leads. A lead has a
  manager's powers over the project they run, and is scoped the same way: to that project.
* **Acting on a person's own request** — decided by that person's tier, so requests travel
  up rather than sideways. Managers (PM / project manager / HR) go to an admin; a lead goes
  to their project's PM; everyone else to their project's PM or lead.

Together those give a lead full authority over their members, none over a peer lead, and a
manager's own leave nobody on the project can sign off.

The PM of a project is an ``employees.id`` living in a JSON column, so these also cover the
mistakes that shape invites: comparing a ``users.id`` against it, ignoring the
organisation-level fallback, and choking on the junk that accumulates in a column with no
foreign key.
"""
import os
import sys
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.models.allocation       # noqa: F401
import app.models.employee         # noqa: F401
import app.models.parent_project   # noqa: F401
import app.models.project          # noqa: F401
import app.models.sub_project      # noqa: F401
import app.models.user             # noqa: F401

from app.db.database import Base
from app.models.allocation import Allocation
from app.models.employee import Employee
from app.models.parent_project import MainProject
from app.models.project import DailySheet
from app.models.user import User
from app.services import project_scope


# Employee ids. Deliberately far from the user ids below so a users.id/employees.id
# mix-up cannot pass by coincidence.
PM_A_EMP = 501          # PM of project A
PM_B_EMP = 502          # PM of project B, and a team lead on A
LEAD_EMP = 503          # team lead only, no project of their own
WORKER_EMP = 504        # ordinary member of both projects
ORG_PM_EMP = 505        # PM at organisation level only
PEER_LEAD_EMP = 506     # a second team lead on project A, alongside LEAD_EMP


@pytest.fixture(scope="module")
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()

    # Employee rows for every persona, created up front. Tests that add their own would
    # otherwise collide with the auto-increment ids of the parametrised cases below, and
    # depending on another test having inserted a row makes the suite order-dependent.
    session.add_all([
        Employee(id=PM_A_EMP, name="PM A", email="pma@x.com",
                 employee_type="Full-time", designation="Program Manager"),
        Employee(id=PM_B_EMP, name="PM B", email="pmb@x.com",
                 employee_type="Full-time", designation="Program Manager"),
        Employee(id=LEAD_EMP, name="Lead", email="lead@x.com",
                 employee_type="Full-time", designation="Team Lead"),
        Employee(id=WORKER_EMP, name="Worker", email="worker@x.com",
                 employee_type="Full-time", designation="Annotator/ Reviewer"),
        Employee(id=ORG_PM_EMP, name="Org PM", email="orgpm@x.com",
                 employee_type="Full-time", designation="Program Manager"),
        Employee(id=PEER_LEAD_EMP, name="Peer Lead", email="peer@x.com",
                 employee_type="Full-time", designation="Team Lead"),
    ])

    # Organisation with a PM of its own, used for the fallback case.
    org = MainProject(id=900, name="Acme", program_manager_ids=[ORG_PM_EMP])
    session.add(org)

    common = dict(
        client="Acme", project_type="Full", total_tasks=10,
        estimated_time_per_task=1.0, start_date=date(2026, 1, 1),
    )
    session.add_all([
        # Names its own PM, so the organisation's PM must NOT apply.
        DailySheet(id=1, name="Project A", main_project_id=900,
                   assigned_employee_ids=[PM_A_EMP], **common),
        DailySheet(id=2, name="Project B", main_project_id=900,
                   assigned_employee_ids=[PM_B_EMP], **common),
        # No project-level PM: the organisation's PM inherits it.
        DailySheet(id=3, name="Legacy", main_project_id=900,
                   assigned_employee_ids=[], **common),
        # No PM anywhere.
        DailySheet(id=4, name="Orphan", main_project_id=None,
                   assigned_employee_ids=[], **common),
    ])

    session.add_all([
        # PM B is lent to project A as a team lead — the tag is descriptive only.
        Allocation(employee_id=PM_B_EMP, sub_project_id=1, role_tags=["Team Lead"]),
        Allocation(employee_id=LEAD_EMP, sub_project_id=1, role_tags=["Team Lead"]),
        # A second lead on the same project, so "may one lead decide for another?" can be
        # asked at all.
        Allocation(employee_id=PEER_LEAD_EMP, sub_project_id=1, role_tags=["Team Lead"]),
        Allocation(employee_id=WORKER_EMP, sub_project_id=1, role_tags=["Annotation"]),
        Allocation(employee_id=WORKER_EMP, sub_project_id=2, role_tags=["Review"]),
    ])
    session.commit()
    yield session
    session.close()


def user(role, employee_id, user_id=1):
    return User(id=user_id, email="x@y.z", name="X", password_hash="x",
                role=role, employee_id=employee_id)


def project(db, project_id):
    return db.query(DailySheet).filter(DailySheet.id == project_id).first()


# ── Resolving a project's PMs ───────────────────────────────────────

def test_project_level_pm_wins_over_organisation(db):
    """A project naming its own PM is scoped to them, not to the org's PM.

    Without this, every PM in an organisation could act on every project under it —
    the case that appears once two PMs share one client.
    """
    assert project_scope.project_pm_ids(db, project(db, 1)) == {PM_A_EMP}
    assert ORG_PM_EMP not in project_scope.project_pm_ids(db, project(db, 1))


def test_organisation_pm_inherited_when_project_names_none(db):
    assert project_scope.project_pm_ids(db, project(db, 3)) == {ORG_PM_EMP}


def test_no_pm_anywhere_resolves_empty_not_error(db):
    assert project_scope.project_pm_ids(db, project(db, 4)) == set()
    assert project_scope.project_pm_ids(db, None) == set()


def test_legacy_single_pm_column_is_honoured(db):
    """Older organisations carry program_manager_id rather than the JSON list."""
    org = MainProject(id=901, name="Legacy Org", program_manager_id=777)
    sheet = DailySheet(id=5, name="P", main_project_id=901, assigned_employee_ids=[],
                       client="c", project_type="Full", total_tasks=1,
                       estimated_time_per_task=1.0, start_date=date(2026, 1, 1))
    db.add_all([org, sheet])
    db.commit()
    assert project_scope.project_pm_ids(db, sheet) == {777}


def test_unusable_ids_are_dropped_not_raised(db):
    """The id columns are JSON with no FK, so they collect strings and junk.

    A malformed entry must not 500 an approval — and a numeric string still has to
    match, because form posts submit ids as text.
    """
    sheet = DailySheet(id=6, name="P", main_project_id=None,
                       assigned_employee_ids=["501", None, "oops", 502],
                       client="c", project_type="Full", total_tasks=1,
                       estimated_time_per_task=1.0, start_date=date(2026, 1, 1))
    db.add(sheet)
    db.commit()
    assert project_scope.project_pm_ids(db, sheet) == {501, 502}


# ── Cardinality ─────────────────────────────────────────────────────
# Nothing is one-to-one. A project takes several managers and several leads; a manager and a
# lead each take several projects. Pinned because a single-value assumption anywhere —
# pm_ids[0], "the" lead, a uniqueness constraint — silently drops people from decisions they
# are entitled to make, and production already has a project with eight managers.


def test_a_project_may_have_several_managers_and_several_leads(db):
    """No 1-PM-per-project rule: live data violates it and the cleanup is manual."""
    org = MainProject(id=910, name="Crowded", program_manager_ids=[])
    sheet = DailySheet(id=10, name="Crowded project", main_project_id=910,
                       assigned_employee_ids=[PM_A_EMP, PM_B_EMP, ORG_PM_EMP],
                       client="c", project_type="Full", total_tasks=1,
                       estimated_time_per_task=1.0, start_date=date(2026, 1, 1))
    db.add_all([org, sheet])
    db.add_all([
        Allocation(employee_id=LEAD_EMP, sub_project_id=10, role_tags=["Team Lead"]),
        Allocation(employee_id=PEER_LEAD_EMP, sub_project_id=10, role_tags=["Team Lead"]),
    ])
    db.commit()

    assert project_scope.project_pm_ids(db, sheet) == {PM_A_EMP, PM_B_EMP, ORG_PM_EMP}
    assert project_scope.project_lead_ids(db, sheet) == {LEAD_EMP, PEER_LEAD_EMP}
    # Every one of the five may act on it.
    for employee_id, role in (
        (PM_A_EMP, "pm"), (PM_B_EMP, "pm"), (ORG_PM_EMP, "pm"),
        (LEAD_EMP, "team_lead"), (PEER_LEAD_EMP, "team_lead"),
    ):
        assert project_scope.can_act_on_project(db, user(role, employee_id), sheet)


def test_one_lead_may_lead_several_projects(db):
    """LEAD_EMP leads project 1 and project 10, and decides for members of both."""
    assert project_scope.can_act_on_project(db, user("team_lead", LEAD_EMP), project(db, 1))
    assert project_scope.can_act_on_project(db, user("team_lead", LEAD_EMP), project(db, 10))

    member = Employee(name="Member Ten", email="m10@x.com",
                      employee_type="Full-time", designation="Annotator/ Reviewer")
    db.add(member)
    db.commit()
    db.refresh(member)
    db.add(Allocation(employee_id=member.id, sub_project_id=10, role_tags=["Annotation"]))
    db.commit()

    lead = user("team_lead", LEAD_EMP)
    assert project_scope.can_manage_employee(db, lead, member.id), "member of project 10"
    assert project_scope.can_manage_employee(db, lead, WORKER_EMP), "member of project 1"


def test_one_manager_may_manage_several_projects(db):
    """PM_A manages project 1 and project 10 at once."""
    pm_a = user("pm", PM_A_EMP)
    assert project_scope.can_act_on_project(db, pm_a, project(db, 1))
    assert project_scope.can_act_on_project(db, pm_a, project(db, 10))
    assert not project_scope.can_act_on_project(db, pm_a, project(db, 2))


def test_being_on_two_projects_means_either_side_may_decide(db):
    """WORKER is on projects 1 and 2, so PM_A and PM_B can each decide for them.

    Attribution comes from approved_by plus the audit entry, not from narrowing this.
    """
    assert project_scope.can_manage_employee(db, user("pm", PM_A_EMP), WORKER_EMP)
    assert project_scope.can_manage_employee(db, user("pm", PM_B_EMP), WORKER_EMP)


def test_a_lead_on_several_projects_may_be_decided_by_any_of_their_managers(db):
    """LEAD_EMP leads projects 1 and 10, whose managers differ.

    Any one of those managers may action the lead's own request — the rule asks "is the
    caller a PM of *some* project this person is on", not of one designated project. Which
    manager actually decided is recorded on the row and in the audit entry, so no narrowing
    is needed to keep it accountable.
    """
    lead_id = LEAD_EMP
    assert project_scope.escalates_to_pm(db, lead_id)

    # Project 1 is managed by PM_A; project 10 by PM_A, PM_B and ORG_PM.
    for manager_id in (PM_A_EMP, PM_B_EMP, ORG_PM_EMP):
        assert project_scope.can_manage_employee(
            db, user("pm", manager_id), lead_id
        ), f"manager {manager_id} shares a project with this lead"

    # A manager of neither project still cannot.
    outsider = Employee(name="Outside PM", email="outpm@x.com",
                        employee_type="Full-time", designation="Program Manager")
    db.add(outsider)
    db.commit()
    db.refresh(outsider)
    assert not project_scope.can_manage_employee(db, user("pm", outsider.id), lead_id)


# ── Who may decide a person's own request ───────────────────────────

def test_nobody_below_admin_may_action_their_own_request(db):
    """A manager and a lead are both refused their own leave, and for different reasons.

    The manager tier stops a PM approving themselves; the lead tier stops a lead, because
    deciding a lead's request needs the project's *PM* and a lead is never in that set. Worth
    pinning as one test: it is the same user-visible rule, and if either tier were relaxed the
    self-approval hole would reopen silently.
    """
    assert not project_scope.can_manage_employee(
        db, user("pm", PM_A_EMP), PM_A_EMP
    ), "a program manager must not approve their own request"
    assert not project_scope.can_manage_employee(
        db, user("team_lead", LEAD_EMP), LEAD_EMP
    ), "a team lead must not approve their own request"


@pytest.mark.parametrize("actor_role", ["admin", "hr"])
def test_hr_and_admin_may_action_every_tier(db, actor_role):
    """HR carries combined Admin + PM access, so no tier is closed to it.

    Includes a program manager's own request, which every project-level caller is refused.
    """
    actor = user(actor_role, None)
    for subject_id, label in (
        (PM_A_EMP, "a program manager"),
        (LEAD_EMP, "a team lead"),
        (WORKER_EMP, "an ordinary member"),
    ):
        assert project_scope.can_manage_employee(db, actor, subject_id), label


# ── The four roles ──────────────────────────────────────────────────

def test_pm_may_act_on_own_project(db):
    assert project_scope.is_project_pm(db, user("pm", PM_A_EMP), project(db, 1))


def test_pm_may_not_act_on_someone_elses_project(db):
    assert not project_scope.is_project_pm(db, user("pm", PM_A_EMP), project(db, 2))


def test_lead_may_act_on_the_project_they_lead(db):
    """A lead has a manager's powers over their own project."""
    lead = user("team_lead", LEAD_EMP)
    assert project_scope.can_act_on_project(db, lead, project(db, 1))


def test_lead_may_not_act_on_a_project_they_do_not_lead(db):
    """Same scoping as a PM: powers stop at the projects you actually run."""
    lead = user("team_lead", LEAD_EMP)
    for project_id in (2, 3, 4):
        assert not project_scope.can_act_on_project(db, lead, project(db, project_id))


def test_lead_is_not_the_projects_pm(db):
    """The stricter test still separates the ranks.

    Both may act on the project; only the PM may decide a *lead's* own requests, so the two
    checks have to stay distinct.
    """
    lead = user("team_lead", LEAD_EMP)
    assert project_scope.can_act_on_project(db, lead, project(db, 1))
    assert not project_scope.is_project_pm(db, lead, project(db, 1))


def test_pm_lent_as_a_lead_may_act_on_both_projects(db):
    """PM B manages project B and leads project A, so both are theirs to act on.

    Their *rank* still differs between the two — see the escalation tests below.
    """
    pm_b = user("pm", PM_B_EMP)
    assert project_scope.can_act_on_project(db, pm_b, project(db, 2)), "own project"
    assert project_scope.can_act_on_project(db, pm_b, project(db, 1)), "led project"
    assert not project_scope.is_project_pm(db, pm_b, project(db, 1)), "not its manager"


@pytest.mark.parametrize("role", ["admin", "hr"])
def test_admin_and_hr_bypass_scoping(db, role):
    """Including on a project with no PM at all, where every other role fails."""
    elevated = user(role, None)
    assert project_scope.can_act_on_project(db, elevated, project(db, 4))
    assert project_scope.can_manage_employee(db, elevated, WORKER_EMP)


def test_the_role_tag_alone_makes_someone_a_lead(db):
    """A tagged allocation is what makes a borrowed manager this project's lead.

    Their designation says "Program Manager", so nothing else on the row would.
    """
    assert PM_B_EMP in project_scope.project_lead_ids(db, project(db, 1))
    assert project_scope.can_act_on_project(db, user("pm", PM_B_EMP), project(db, 1))


def test_an_ordinary_allocation_does_not_make_someone_a_lead(db):
    """Otherwise every annotator on a project could act on it."""
    assert WORKER_EMP not in project_scope.project_lead_ids(db, project(db, 1))
    assert not project_scope.can_act_on_project(db, user("employee", WORKER_EMP), project(db, 1))


# ── Scoping by employee (leaves, WFH, evaluations) ──────────────────

def test_pm_may_not_act_on_an_unrelated_employee(db):
    assert not project_scope.can_manage_employee(db, user("pm", PM_A_EMP), 9999)


# ── Requests that escalate past the project PM to an admin ──────────

def test_a_program_managers_own_request_is_admin_only(db):
    """Even for the PM of the project they are allocated to as a temporary team lead.

    PM B sits on project A as a lead, so PM A manages a project PM B is on — without the
    designation rule, PM A could approve PM B's leave.
    """
    assert project_scope.escalates_to_admin(db, PM_B_EMP)
    assert PM_B_EMP in {
        a.employee_id
        for a in db.query(Allocation).filter(Allocation.sub_project_id == 1).all()
    }, "PM B really is allocated to project A"
    assert not project_scope.can_manage_employee(db, user("pm", PM_A_EMP), PM_B_EMP)


def test_a_team_leads_own_request_goes_to_their_project_pm(db):
    """The other half of the rule: a lead is *not* admin-only, their PM decides."""
    assert not project_scope.escalates_to_admin(db, LEAD_EMP)
    assert project_scope.can_manage_employee(db, user("pm", PM_A_EMP), LEAD_EMP)


def test_escalation_is_keyed_on_designation_not_users_role(db):
    """Production has people designated "Program Manager" whose users.role is "employee".

    Keying on the role would route their requests to a project PM; the designation is the
    source of truth that derives the role, so it decides.
    """
    drifted = Employee(id=520, name="Drifted", email="drift@x.com",
                       employee_type="Full-time", designation="Program Manager")
    db.add(drifted)
    db.add(Allocation(employee_id=520, sub_project_id=1, role_tags=["Annotation"]))
    db.commit()

    assert project_scope.escalates_to_admin(db, 520)
    assert not project_scope.can_manage_employee(db, user("pm", PM_A_EMP), 520)


@pytest.mark.parametrize(
    "designation,admin_only,pm_only",
    [
        # The hierarchy itself, in one table, so a change to it shows up in the diff.
        ("Program Manager", True, False),
        ("HR", True, False),
        ("Team Lead", False, True),     # escalates to their PM, not to an admin
        ("Annotator/ Reviewer", False, False),
        ("Other", False, False),
        (None, False, False),
        # ...and the spellings it has to survive. The column is unconstrained free text, so
        # a variant that slipped through would fail *silently* — that person's leave would
        # quietly become approvable a tier below.
        ("Project Manager", True, False),   # used interchangeably with "Program Manager"
        ("  program manager  ", True, False),
        ("PROGRAM MANAGER", True, False),
        ("  team lead  ", False, True),
    ],
)
def test_tier_per_designation(db, designation, admin_only, pm_only):
    subject = Employee(name=f"T {designation}",
                       email=f"tier{abs(hash(str(designation)))}@x.com",
                       employee_type="Full-time", designation=designation)
    db.add(subject)
    db.commit()
    db.refresh(subject)
    assert project_scope.escalates_to_admin(db, subject.id) is admin_only
    assert project_scope.escalates_to_pm(db, subject.id) is pm_only


def test_escalation_handles_unknown_and_missing_employees(db):
    assert not project_scope.escalates_to_admin(db, 999999)
    assert not project_scope.escalates_to_admin(db, None)
    assert not project_scope.escalates_to_pm(db, 999999)
    assert not project_scope.escalates_to_pm(db, None)


def test_the_three_tiers_are_mutually_exclusive(db):
    """A designation must not land in both escalation sets, or the order of the checks in
    can_manage_employee would silently decide which rule wins."""
    assert not (
        project_scope.ADMIN_ONLY_SUBJECT_DESIGNATIONS
        & project_scope.PM_ONLY_SUBJECT_DESIGNATIONS
    )


def test_lead_may_decide_for_a_member_of_their_project(db):
    """The point of the change: a lead decides for their own team, like a PM."""
    assert project_scope.can_manage_employee(db, user("team_lead", LEAD_EMP), WORKER_EMP)


def test_lead_may_not_decide_for_someone_outside_their_project(db):
    assert not project_scope.can_manage_employee(db, user("team_lead", LEAD_EMP), 9999)


def test_user_id_is_never_mistaken_for_employee_id(db):
    """A caller whose users.id happens to equal a PM's employees.id must still fail.

    The two are unrelated sequences; comparing them is the mistake this guards.
    """
    impostor = user("pm", employee_id=None, user_id=PM_A_EMP)
    assert not project_scope.is_project_pm(db, impostor, project(db, 1))
    assert not project_scope.can_manage_employee(db, impostor, WORKER_EMP)


def test_caller_without_an_employee_record_is_denied(db):
    assert not project_scope.is_project_pm(db, user("pm", None), project(db, 1))


# ── Guards ──────────────────────────────────────────────────────────

def test_each_tier_gets_a_distinct_403(db):
    """Three refusals with three different fixes, so the message has to say which.

    "You don't manage this project" is misleading when the caller *does* manage it and is
    simply the wrong rank to decide.
    """
    # Wrong project.
    with pytest.raises(HTTPException) as outside:
        project_scope.require_employee_scope(
            db, user("pm", PM_A_EMP), 9999, action="approve leave"
        )
    assert outside.value.status_code == 403
    assert "you manage" in outside.value.detail

    # Subject is a manager → admin only.
    with pytest.raises(HTTPException) as manager_subject:
        project_scope.require_employee_scope(
            db, user("pm", PM_A_EMP), PM_B_EMP, action="approve leave"
        )
    assert "only an admin" in manager_subject.value.detail

    # Subject is a lead → their PM only. Asked by a *peer lead* on the same project.
    with pytest.raises(HTTPException) as lead_subject:
        project_scope.require_employee_scope(
            db, user("team_lead", PEER_LEAD_EMP), LEAD_EMP, action="approve leave"
        )
    assert "program manager" in lead_subject.value.detail


def test_a_peer_lead_may_not_decide_for_another_lead(db):
    """Requests travel up, never sideways — the hierarchy the role was added for.

    Both leads sit on project 1, so without the tier rule each could approve the other.
    """
    assert project_scope.can_act_on_project(db, user("team_lead", PEER_LEAD_EMP), project(db, 1))
    assert not project_scope.can_manage_employee(db, user("team_lead", PEER_LEAD_EMP), LEAD_EMP)
    # ...while the project's actual PM can.
    assert project_scope.can_manage_employee(db, user("pm", PM_A_EMP), LEAD_EMP)


def test_require_project_scope_allows_pm_and_lead_and_blocks_outsiders(db):
    project_scope.require_project_scope(db, user("pm", PM_A_EMP), project(db, 1))
    project_scope.require_project_scope(db, user("team_lead", LEAD_EMP), project(db, 1))
    with pytest.raises(HTTPException) as exc:
        project_scope.require_project_scope(db, user("pm", PM_A_EMP), project(db, 2))
    assert exc.value.status_code == 403
