from datetime import datetime, timezone, timedelta
import logging
from typing import Any, Iterable, Optional, Dict, Tuple, List
from sqlalchemy import true, false, literal, null, select, TIMESTAMP, or_, func, case
from sqlalchemy.orm import Session, load_only
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.constants.designations import (
    ANNOTATOR_REVIEWER_DESIGNATIONS,
    DEVELOPER_DESIGNATIONS,
    PM_DESIGNATIONS,
    TEAM_LEAD_DESIGNATIONS,
)
from app.models.employee import Employee
from app.models.cluster import Cluster, ClusterMember
from app.models.assignment import Assignment
from app.models.assessment import Assessment, AssessmentAttempt, AssessmentEnrollment
from app.services.assessment_lifecycle import utc

logger = logging.getLogger(__name__)

# Exact stored string values shared with app/api/employees.py via app/constants/designations.py
DYNAMIC_SCOPE_FILTERS = {
    "everyone": lambda c: true(),
    "full_time": lambda c: Employee.employee_type == "Full-time",
    "annotators_reviewers": lambda c: Employee.designation.in_(ANNOTATOR_REVIEWER_DESIGNATIONS),
    "developers": lambda c: Employee.designation.in_(DEVELOPER_DESIGNATIONS),
    "project_managers": lambda c: Employee.designation.in_(PM_DESIGNATIONS),
    "team_leads": lambda c: Employee.designation.in_(TEAM_LEAD_DESIGNATIONS),
    "new_joiners": lambda c: (
        (Employee.date_of_joining.isnot(None)) & (Employee.date_of_joining >= c.joined_on_or_after)
        if c.joined_on_or_after else false()
    ),
}


def _dialect_name(db: Session) -> str:
    bind = db.get_bind() if hasattr(db, "get_bind") else getattr(db, "bind", None)
    return bind.dialect.name if bind is not None else "sqlite"


def get_eligible_employees_query(db: Session, cluster: Cluster):
    scopes = [s for s in getattr(cluster, "effective_scopes", [cluster.dynamic_scope]) if s != "custom"]
    if not scopes:
        raise ValueError(f"Unknown cluster dynamic scope: {cluster.dynamic_scope}")

    predicates = []
    for scope in scopes:
        if scope not in DYNAMIC_SCOPE_FILTERS:
            raise ValueError(f"Unknown cluster dynamic scope: {scope}")
        predicates.append(DYNAMIC_SCOPE_FILTERS[scope](cluster))

    rule = predicates[0] if len(predicates) == 1 else or_(*predicates)
    # Includes employees on leave so they don't lose assigned modules; excludes archived & inactive
    return db.query(Employee.id).filter(
        Employee.status.in_(["active", "on-leave"]),
        rule,
    )


def populate_cluster_members(db: Session, cluster: Cluster) -> int:
    """
    Populates cluster_members using dialect-aware INSERT ... ON CONFLICT DO NOTHING.
    Cross-DB portable for both PostgreSQL production and SQLite testing.
    """
    if not cluster.is_dynamic:
        return 0

    eligible_subquery = get_eligible_employees_query(db, cluster).with_entities(
        literal(cluster.id).label("cluster_id"),
        Employee.id.label("employee_id"),
    )
    insert_fn = pg_insert if _dialect_name(db) == "postgresql" else sqlite_insert
    stmt = (
        insert_fn(ClusterMember)
        .from_select(["cluster_id", "employee_id"], eligible_subquery.statement)
        .on_conflict_do_nothing(index_elements=["cluster_id", "employee_id"])
    )
    result = db.execute(stmt)
    return result.rowcount if hasattr(result, "rowcount") and result.rowcount and result.rowcount > 0 else 0


def fan_out_cluster(
    db: Session,
    cluster: Cluster,
    now: Optional[datetime] = None,
    target_employee_id: Optional[int] = None,
) -> int:
    """
    Fans out active published assessment assignments for a cluster to active/on-leave
    roster members using set-based SQL operations (no N+1 query loops).
    When ``target_employee_id`` is provided, scopes operations strictly to that member.
    """
    if cluster.is_archived:
        return 0

    if now is None:
        now = datetime.now(timezone.utc)

    assignments = (
        db.query(Assignment, Assessment.max_attempts)
        .join(Assessment, Assessment.id == Assignment.assessment_id)
        .filter(
            Assignment.cluster_id == cluster.id,
            Assignment.is_active == True,
            Assessment.status == "published",
        )
        .all()
    )
    if not assignments:
        return 0

    insert_fn = pg_insert if _dialect_name(db) == "postgresql" else sqlite_insert

    member_filters = [
        ClusterMember.cluster_id == cluster.id,
        Employee.status.in_(["active", "on-leave"]),
    ]
    if target_employee_id is not None:
        emp_ids = [target_employee_id]
    else:
        emp_ids = [
            row[0]
            for row in db.query(ClusterMember.employee_id)
            .join(Employee, Employee.id == ClusterMember.employee_id)
            .filter(*member_filters)
            .all()
        ]
    if not emp_ids:
        return 0

    total_created = 0
    for a, asm_max_attempts in assignments:
        if not a.assessment_id:
            continue

        # For fixed deadlines: skip if baseline has already passed
        if a.relative_due_days is None and utc(a.deadline) and utc(a.deadline) <= now:
            continue

        max_att = max(int(asm_max_attempts or 1), 1)
        target_due = (now + timedelta(days=a.relative_due_days)) if a.relative_due_days else None

        # 1. Set-based revive of previously cancelled enrollments for returning employees.
        # Clear any stale deadline when reviving onto a fixed assignment so it inherits assignments.deadline.
        revive_values = {
            "status": "assigned",
            "assignment_id": a.id,
            "deadline": target_due,
            "max_attempts_snapshot": max_att,
            "updated_at": now,
        }

        revived = (
            db.query(AssessmentEnrollment)
            .filter(
                AssessmentEnrollment.assessment_id == a.assessment_id,
                AssessmentEnrollment.employee_id.in_(emp_ids),
                AssessmentEnrollment.status == "cancelled",
            )
            .update(revive_values, synchronize_session=False)
        )

        # 2. Link orphaned active enrollments and populate missing snapshots / deadlines
        link_values = {
            "assignment_id": a.id,
            "max_attempts_snapshot": max_att,
            "updated_at": now,
        }
        if target_due is not None:
            link_values["deadline"] = target_due

        db.query(AssessmentEnrollment).filter(
            AssessmentEnrollment.assessment_id == a.assessment_id,
            AssessmentEnrollment.employee_id.in_(emp_ids),
            or_(
                AssessmentEnrollment.assignment_id.is_(None),
                AssessmentEnrollment.max_attempts_snapshot.is_(None),
            ),
        ).update(link_values, synchronize_session=False)

        # 3. Set-based bulk INSERT ... SELECT ... ON CONFLICT (employee_id, assessment_id) DO NOTHING
        deadline_col = (
            literal(target_due, type_=TIMESTAMP(timezone=True))
            if target_due is not None
            else null()
        ).label("deadline")

        select_stmt = (
            select(
                literal(a.id).label("assignment_id"),
                literal(a.assessment_id).label("assessment_id"),
                ClusterMember.employee_id.label("employee_id"),
                literal("assigned").label("status"),
                deadline_col,
                literal(max_att).label("max_attempts_snapshot"),
            )
            .select_from(ClusterMember)
            .where(
                ClusterMember.cluster_id == cluster.id,
                ClusterMember.employee_id.in_(emp_ids),
            )
        )
        stmt = (
            insert_fn(AssessmentEnrollment)
            .from_select(
                [
                    "assignment_id",
                    "assessment_id",
                    "employee_id",
                    "status",
                    "deadline",
                    "max_attempts_snapshot",
                ],
                select_stmt,
            )
            .on_conflict_do_nothing(index_elements=["employee_id", "assessment_id"])
        )
        result = db.execute(stmt)
        inserted = result.rowcount if hasattr(result, "rowcount") and result.rowcount and result.rowcount > 0 else 0
        total_created += inserted + (revived or 0)

    db.flush()
    return total_created


def fan_out_assignments_for_cluster(db: Session, cluster_id: int) -> int:
    """Convenience wrapper accepting cluster_id."""
    cluster = db.query(Cluster).filter_by(id=cluster_id).first()
    if not cluster:
        return 0
    return fan_out_cluster(db, cluster)


def find_alternate_active_assignment(
    db: Session,
    employee_id: int,
    assessment_id: int,
    exclude_assignment_id: Optional[int] = None,
) -> Optional[Assignment]:
    """
    Checks if employee is part of another active cluster that has an active assignment
    for the same published assessment.
    """
    query = (
        db.query(Assignment)
        .join(Cluster, Cluster.id == Assignment.cluster_id)
        .join(ClusterMember, ClusterMember.cluster_id == Cluster.id)
        .join(Assessment, Assessment.id == Assignment.assessment_id)
        .filter(
            ClusterMember.employee_id == employee_id,
            Cluster.is_archived == False,
            Assignment.is_active == True,
            Assignment.assessment_id == assessment_id,
            Assessment.status == "published",
        )
        .order_by(Assignment.id.asc())
    )
    if exclude_assignment_id:
        query = query.filter(Assignment.id != exclude_assignment_id)
    return query.first()


def find_alternate_active_assignments_bulk(
    db: Session,
    employee_ids: Iterable[int],
    assessment_ids: Iterable[int],
    exclude_assignment_ids: Optional[Iterable[int]] = None,
) -> Dict[Tuple[int, int], Assignment]:
    """
    Batched bulk lookup: checks for alternative active assignments for (employee_id, assessment_id) pairs
    in a single SQL query, returning a dict mapping (employee_id, assessment_id) -> Assignment (lowest id).
    Eliminates N+1 query cascades during bulk member removals and assignment deactivations.
    """
    emp_ids = list(set(employee_ids))
    asg_asmt_ids = list(set(assessment_ids))
    if not emp_ids or not asg_asmt_ids:
        return {}

    query = (
        db.query(ClusterMember.employee_id, Assignment)
        .join(Cluster, Cluster.id == Assignment.cluster_id)
        .join(ClusterMember, ClusterMember.cluster_id == Cluster.id)
        .join(Assessment, Assessment.id == Assignment.assessment_id)
        .filter(
            ClusterMember.employee_id.in_(emp_ids),
            Cluster.is_archived == False,
            Assignment.is_active == True,
            Assignment.assessment_id.in_(asg_asmt_ids),
            Assessment.status == "published",
        )
        .order_by(Assignment.id.asc())
    )
    if exclude_assignment_ids:
        query = query.filter(Assignment.id.notin_(list(set(exclude_assignment_ids))))

    results: Dict[Tuple[int, int], Assignment] = {}
    for emp_id, asg in query.all():
        key = (emp_id, asg.assessment_id)
        if key not in results:
            results[key] = asg
    return results


def _get_attempted_enrollment_ids(db: Session, enrollment_ids: Iterable[int]) -> set[int]:
    """Batch-fetch the subset of enrollment IDs that already have at least one sitting."""
    id_list = list(set(enrollment_ids))
    if not id_list:
        return set()
    return {
        row[0]
        for row in db.query(AssessmentAttempt.enrollment_id)
        .filter(AssessmentAttempt.enrollment_id.in_(id_list))
        .distinct()
        .all()
    }


_UNSET = object()


def _repoint_or_cancel_enrollment(
    db: Session,
    enrollment: AssessmentEnrollment,
    source_assignment: Assignment,
    now: datetime,
    alt: Any = _UNSET,
    has_attempts: Optional[bool] = None,
) -> None:
    """Re-points enrollment to an overlapping active assignment or cancels unstarted ones."""
    if alt is _UNSET:
        alt = find_alternate_active_assignment(
            db,
            enrollment.employee_id,
            source_assignment.assessment_id,
            exclude_assignment_id=source_assignment.id,
        )
    if alt:
        enrollment.assignment_id = alt.id
        # Reconcile deadlines cleanly when switching between rolling & fixed schedules
        # Never-Shorten rule: only clear override if the candidate extension was <= alt.deadline
        if source_assignment.relative_due_days is not None and alt.relative_due_days is None:
            if not enrollment.deadline or (alt.deadline and enrollment.deadline <= alt.deadline):
                enrollment.deadline = None
        elif alt.relative_due_days is not None and enrollment.deadline is None:
            enrollment.deadline = now + timedelta(days=alt.relative_due_days)
        elif alt.relative_due_days is None and source_assignment.relative_due_days is None:
            if alt.deadline and enrollment.deadline and enrollment.deadline <= alt.deadline:
                enrollment.deadline = None
        enrollment.updated_at = now
    else:
        if enrollment.status == "assigned":
            if has_attempts is None:
                has_attempts = bool(
                    db.query(AssessmentAttempt.id)
                    .filter(AssessmentAttempt.enrollment_id == enrollment.id)
                    .first()
                )
            if has_attempts:
                # Preserve assignment_id & historical attempts/logs/videos under original cluster in Progress & Logs
                enrollment.status = "failed"
                enrollment.updated_at = now
            else:
                enrollment.status = "cancelled"
                enrollment.assignment_id = None
                enrollment.updated_at = now


def _bulk_repoint_or_cancel_enrollments(
    db: Session,
    enrollments: List[AssessmentEnrollment],
    source_assignments_by_id: Dict[int, Assignment],
    now: datetime,
) -> None:
    """
    O(1)-query batch deduplication across N enrollments and K deactivated assignments:
    - 1 query for alternate active assignments across all (employee, assessment) pairs
    - 1 query for attempted enrollment IDs (only for 'assigned' enrollments without an alternate)
    - At most 2 set-based UPDATE statements for cancellations and attempted->failed transitions
    """
    if not enrollments:
        return

    emp_ids = list({enr.employee_id for enr in enrollments})
    asmt_ids = list({enr.assessment_id for enr in enrollments if enr.assessment_id})
    exclude_asg_ids = list(source_assignments_by_id.keys())

    alt_map = find_alternate_active_assignments_bulk(
        db,
        employee_ids=emp_ids,
        assessment_ids=asmt_ids,
        exclude_assignment_ids=exclude_asg_ids,
    )

    assigned_no_alt_ids: List[int] = []
    for enr in enrollments:
        src_asg = source_assignments_by_id.get(enr.assignment_id)
        if not src_asg:
            continue
        alt = alt_map.get((enr.employee_id, enr.assessment_id))
        if alt is not None:
            _repoint_or_cancel_enrollment(db, enr, src_asg, now, alt=alt)
        elif enr.status == "assigned":
            assigned_no_alt_ids.append(enr.id)

    if assigned_no_alt_ids:
        attempted_enr_ids = _get_attempted_enrollment_ids(db, assigned_no_alt_ids)
        fail_ids = [eid for eid in assigned_no_alt_ids if eid in attempted_enr_ids]
        cancel_ids = [eid for eid in assigned_no_alt_ids if eid not in attempted_enr_ids]

        if cancel_ids:
            db.query(AssessmentEnrollment).filter(
                AssessmentEnrollment.id.in_(cancel_ids)
            ).update(
                {"status": "cancelled", "assignment_id": None, "updated_at": now},
                synchronize_session=False,
            )
        if fail_ids:
            db.query(AssessmentEnrollment).filter(
                AssessmentEnrollment.id.in_(fail_ids)
            ).update(
                {"status": "failed", "updated_at": now},
                synchronize_session=False,
            )


def remove_cluster_members_bulk(
    db: Session,
    cluster_id: int,
    employee_ids: Iterable[int],
) -> int:
    """Removes a batch of members from a cluster and runs overlapping deduplication cleanly."""
    emp_id_list = list(set(employee_ids))
    if not emp_id_list:
        return 0

    deleted = (
        db.query(ClusterMember)
        .filter(
            ClusterMember.cluster_id == cluster_id,
            ClusterMember.employee_id.in_(emp_id_list),
        )
        .delete(synchronize_session=False)
    )
    if not deleted:
        return 0
    db.flush()

    cluster_assignments = (
        db.query(Assignment)
        .filter(
            Assignment.cluster_id == cluster_id,
            Assignment.is_active == True,
            Assignment.assessment_id.isnot(None),
        )
        .all()
    )
    if not cluster_assignments:
        return deleted

    now = datetime.now(timezone.utc)
    asg_ids = [a.id for a in cluster_assignments]
    asg_by_id = {a.id: a for a in cluster_assignments}
    assessment_ids = list({a.assessment_id for a in cluster_assignments if a.assessment_id})

    enrollments = (
        db.query(AssessmentEnrollment)
        .options(
            load_only(
                AssessmentEnrollment.id,
                AssessmentEnrollment.employee_id,
                AssessmentEnrollment.assessment_id,
                AssessmentEnrollment.assignment_id,
                AssessmentEnrollment.status,
                AssessmentEnrollment.deadline,
            )
        )
        .filter(
            AssessmentEnrollment.employee_id.in_(emp_id_list),
            AssessmentEnrollment.assessment_id.in_(assessment_ids),
            AssessmentEnrollment.assignment_id.in_(asg_ids),
        )
        .all()
    )
    _bulk_repoint_or_cancel_enrollments(db, enrollments, asg_by_id, now)

    db.flush()
    return deleted


def remove_cluster_member(db: Session, cluster_id: int, employee_id: int) -> bool:
    """Removes a single member from a cluster with overlapping cluster deduplication."""
    return remove_cluster_members_bulk(db, cluster_id, [employee_id]) > 0


def reconcile_dynamic_cluster(db: Session, cluster: Cluster) -> int:
    """
    Reconciles a single dynamic cluster's roster (adds eligible, removes ineligible with
    deduplication, and fans out active assignments) within the caller's transaction.
    Returns count of newly added members.
    """
    if cluster.is_archived or not cluster.is_dynamic:
        return 0

    eligible_ids = set(row[0] for row in get_eligible_employees_query(db, cluster).all())
    current_ids = set(
        row[0] for row in db.query(ClusterMember.employee_id).filter_by(cluster_id=cluster.id).all()
    )

    to_remove = current_ids - eligible_ids
    if to_remove:
        remove_cluster_members_bulk(db, cluster.id, to_remove)

    added = populate_cluster_members(db, cluster)
    db.flush()

    fan_out_cluster(db, cluster)
    return added


def deactivate_assignment(db: Session, assignment_id: int) -> bool:
    """
    Soft-deactivates an assignment (is_active = False) and deduplicates enrolled members.
    If member belongs to an alternate cluster with an active assignment for the test,
    re-points their enrollment; otherwise soft-cancels unstarted enrollments.
    """
    assignment = db.query(Assignment).filter_by(id=assignment_id).first()
    if not assignment or not assignment.is_active:
        return False

    now = datetime.now(timezone.utc)
    assignment.is_active = False
    assignment.updated_at = now

    if assignment.assessment_id:
        enrollments = (
            db.query(AssessmentEnrollment)
            .options(
                load_only(
                    AssessmentEnrollment.id,
                    AssessmentEnrollment.employee_id,
                    AssessmentEnrollment.assessment_id,
                    AssessmentEnrollment.assignment_id,
                    AssessmentEnrollment.status,
                    AssessmentEnrollment.deadline,
                )
            )
            .filter_by(assignment_id=assignment.id)
            .all()
        )
        _bulk_repoint_or_cancel_enrollments(db, enrollments, {assignment.id: assignment}, now)

    db.flush()
    return True


def archive_cluster(db: Session, cluster_id: int) -> bool:
    """
    Soft-archives cluster (is_archived = True) and deactivates all active assignments
    in a single O(1)-query batched pass (no N+1 per member or per assignment).
    """
    cluster = db.query(Cluster).filter_by(id=cluster_id).first()
    if not cluster or cluster.is_archived:
        return False

    now = datetime.now(timezone.utc)
    cluster.is_archived = True
    cluster.updated_at = now

    active_asgs = db.query(Assignment).filter_by(cluster_id=cluster.id, is_active=True).all()
    if active_asgs:
        asg_by_id = {a.id: a for a in active_asgs}
        asg_ids = list(asg_by_id.keys())
        db.query(Assignment).filter(Assignment.id.in_(asg_ids)).update(
            {"is_active": False, "updated_at": now},
            synchronize_session=False,
        )
        enrollments = (
            db.query(AssessmentEnrollment)
            .options(
                load_only(
                    AssessmentEnrollment.id,
                    AssessmentEnrollment.employee_id,
                    AssessmentEnrollment.assessment_id,
                    AssessmentEnrollment.assignment_id,
                    AssessmentEnrollment.status,
                    AssessmentEnrollment.deadline,
                )
            )
            .filter(AssessmentEnrollment.assignment_id.in_(asg_ids))
            .all()
        )
        _bulk_repoint_or_cancel_enrollments(db, enrollments, asg_by_id, now)

    db.flush()
    return True


def archive_assessment_cascade(db: Session, assessment_id: int, now: Optional[datetime] = None) -> None:
    """Deactivates active assignments and cancels unstarted enrollments for an archived assessment."""
    if now is None:
        now = datetime.now(timezone.utc)

    db.query(Assignment).filter(
        Assignment.assessment_id == assessment_id,
        Assignment.is_active == True,
    ).update({"is_active": False, "updated_at": now}, synchronize_session=False)

    db.query(AssessmentEnrollment).filter(
        AssessmentEnrollment.assessment_id == assessment_id,
        AssessmentEnrollment.status == "assigned",
        ~AssessmentEnrollment.attempts.any(),
    ).update({"status": "cancelled", "assignment_id": None, "updated_at": now}, synchronize_session=False)

    db.query(AssessmentEnrollment).filter(
        AssessmentEnrollment.assessment_id == assessment_id,
        AssessmentEnrollment.status == "assigned",
        AssessmentEnrollment.attempts.any(),
    ).update({"status": "failed", "updated_at": now}, synchronize_session=False)

    db.flush()


def _reactivate_assignments(
    db: Session,
    candidates: List[Tuple[Assignment, Cluster, Assessment]],
    now: datetime,
) -> int:
    """
    Re-activates non-expired deactivated assignments whose cluster is active and assessment is published,
    skips pairs that already have an active assignment, fans out enrollments, and restores 'assigned'
    status for previously-failed enrollments that still have retakes remaining.
    """
    if not candidates:
        return 0

    cluster_ids = list({c.id for _, c, _ in candidates})
    assessment_ids = list({a.id for _, _, a in candidates})

    existing_active = {
        (row[0], row[1])
        for row in db.query(Assignment.cluster_id, Assignment.assessment_id)
        .filter(
            Assignment.cluster_id.in_(cluster_ids),
            Assignment.assessment_id.in_(assessment_ids),
            Assignment.is_active == True,
        )
        .all()
    }

    picked_by_pair: Dict[Tuple[int, int], Tuple[Assignment, Cluster, Assessment]] = {}
    for asg, cl, asm in sorted(candidates, key=lambda x: (x[0].updated_at or x[0].created_at or now, x[0].id), reverse=True):
        pair = (cl.id, asm.id)
        if pair in existing_active or pair in picked_by_pair:
            continue
        if asg.relative_due_days is None and utc(asg.deadline) and utc(asg.deadline) <= now:
            continue
        picked_by_pair[pair] = (asg, cl, asm)

    if not picked_by_pair:
        return 0

    reactivated_asg_ids: List[int] = []
    clusters_to_fan: Dict[int, Cluster] = {}
    max_att_by_asm: Dict[int, int] = {}
    for asg, cl, asm in picked_by_pair.values():
        asg.is_active = True
        asg.updated_at = now
        reactivated_asg_ids.append(asg.id)
        clusters_to_fan[cl.id] = cl
        max_att_by_asm[asm.id] = max(int(asm.max_attempts or 1), 1)

    db.flush()

    for cl in clusters_to_fan.values():
        fan_out_cluster(db, cl)

    # Restore 'assigned' status for attempted enrollments that still had retakes when archived
    failed_enrs = (
        db.query(AssessmentEnrollment)
        .filter(
            AssessmentEnrollment.assignment_id.in_(reactivated_asg_ids),
            AssessmentEnrollment.status == "failed",
        )
        .all()
    )
    if failed_enrs:
        enr_ids = [e.id for e in failed_enrs]
        attempt_stats = {
            row[0]: (int(row[1] or 0), bool(row[2]))
            for row in db.query(
                AssessmentAttempt.enrollment_id,
                func.count(AssessmentAttempt.id),
                func.max(case((AssessmentAttempt.passed == True, 1), else_=0)),
            )
            .filter(AssessmentAttempt.enrollment_id.in_(enr_ids))
            .group_by(AssessmentAttempt.enrollment_id)
            .all()
        }
        for enr in failed_enrs:
            cnt, passed_any = attempt_stats.get(enr.id, (0, False))
            base_max = enr.max_attempts_snapshot or max_att_by_asm.get(enr.assessment_id, 1)
            allowed = max(int(base_max), 1) + max(int(enr.extra_attempts or 0), 0)
            if passed_any:
                enr.status = "passed"
                enr.updated_at = now
            elif cnt < allowed:
                enr.status = "assigned"
                enr.updated_at = now

    db.flush()
    return len(reactivated_asg_ids)


def reactivate_assignments_for_cluster(
    db: Session, cluster_id: int, now: Optional[datetime] = None
) -> int:
    """Re-activates eligible deactivated assignments when a cluster is restored."""
    if now is None:
        now = datetime.now(timezone.utc)
    candidates = (
        db.query(Assignment, Cluster, Assessment)
        .join(Cluster, Cluster.id == Assignment.cluster_id)
        .join(Assessment, Assessment.id == Assignment.assessment_id)
        .filter(
            Assignment.cluster_id == cluster_id,
            Assignment.is_active == False,
            Cluster.is_archived == False,
            Assessment.status == "published",
        )
        .all()
    )
    return _reactivate_assignments(db, candidates, now)


def reactivate_assignments_for_assessment(
    db: Session, assessment_id: int, now: Optional[datetime] = None
) -> int:
    """Re-activates eligible deactivated assignments when an assessment is restored & published."""
    if now is None:
        now = datetime.now(timezone.utc)
    candidates = (
        db.query(Assignment, Cluster, Assessment)
        .join(Cluster, Cluster.id == Assignment.cluster_id)
        .join(Assessment, Assessment.id == Assignment.assessment_id)
        .filter(
            Assignment.assessment_id == assessment_id,
            Assignment.is_active == False,
            Cluster.is_archived == False,
            Assessment.status == "published",
        )
        .all()
    )
    return _reactivate_assignments(db, candidates, now)


def sync_dynamic_clusters_for_employee(db: Session, employee_id: int) -> None:
    """
    Synchronous post-create, post-update, conversion, or archive hook.
    Flushes pending changes in the caller's session so SQL predicates reflect current state,
    then evaluates employee against all active dynamic clusters inside per-cluster savepoints
    so one error never poisons the caller's transaction. Does NOT commit.
    """
    db.flush()
    employee = db.query(Employee).filter_by(id=employee_id).first()
    if not employee:
        return

    now = datetime.now(timezone.utc)

    # Archived or inactive employees are removed from all clusters and unstarted assessments cancelled
    if employee.status in ("archived", "inactive"):
        db.query(ClusterMember).filter_by(employee_id=employee_id).delete(synchronize_session=False)
        db.query(AssessmentEnrollment).filter_by(
            employee_id=employee_id,
            status="assigned",
        ).update({"status": "cancelled", "assignment_id": None, "updated_at": now}, synchronize_session=False)
        db.flush()
        return

    if employee.status in ("active", "on-leave"):
        dynamic_clusters = (
            db.query(Cluster)
            .filter(
                or_(Cluster.dynamic_scope != "custom", Cluster.dynamic_scopes.isnot(None)),
                Cluster.is_archived == False,
            )
            .all()
        )
        for cl in dynamic_clusters:
            try:
                with db.begin_nested():
                    matches = (
                        get_eligible_employees_query(db, cl)
                        .filter(Employee.id == employee_id)
                        .first()
                    )
                    existing = (
                        db.query(ClusterMember)
                        .filter_by(cluster_id=cl.id, employee_id=employee_id)
                        .first()
                    )
                    if matches and not existing:
                        db.add(ClusterMember(cluster_id=cl.id, employee_id=employee_id))
                        db.flush()
                        fan_out_cluster(db, cl, now=now, target_employee_id=employee_id)
                    elif not matches and existing:
                        remove_cluster_member(db, cl.id, employee_id)
            except Exception as e:
                logger.error(
                    "Dynamic cluster sync error for cluster %s, employee %s: %s",
                    cl.id,
                    employee_id,
                    e,
                    exc_info=True,
                )


def reconcile_dynamic_clusters_job(db: Session) -> None:
    """
    Nightly self-healing job (runs at 02:00 AM IST) reconciling dynamic cluster rosters.
    """
    dynamic_clusters = (
        db.query(Cluster)
        .filter(
            or_(Cluster.dynamic_scope != "custom", Cluster.dynamic_scopes.isnot(None)),
            Cluster.is_archived == False,
        )
        .all()
    )
    for cl in dynamic_clusters:
        try:
            reconcile_dynamic_cluster(db, cl)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error("Error reconciling dynamic cluster %s: %s", cl.id, e, exc_info=True)
