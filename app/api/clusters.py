from datetime import datetime, timezone
import logging
import uuid
from typing import List, Optional
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import func

from app.db.database import get_db
from app.models.user import User
from app.models.employee import Employee
from app.models.allocation import Allocation
from app.models.project import DailySheet
from app.models.cluster import Cluster, ClusterMember
from app.models.assignment import Assignment
from app.constants.project_constants import ARCHIVED_STATUSES
from app.schemas.cluster import (
    ClusterCreate,
    ClusterUpdate,
    ClusterRestore,
    ClusterResponse,
    ClusterMemberAdd,
    ClusterMemberResponse,
    ClusterPickerEmployee,
    ClusterPickerDailySheet,
    ClusterPickerOptionsResponse,
    _normalize_scope_inputs,
)
from app.services import audit_service
from app.services.auth_service import require_assessment_admin
from app.services.project_cache import InMemoryTTLCache
from app.services.cluster_service import (
    populate_cluster_members,
    reconcile_dynamic_cluster,
    fan_out_cluster,
    remove_cluster_member,
    remove_cluster_members_bulk,
    archive_cluster,
    reactivate_assignments_for_cluster,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/clusters", tags=["Clusters"])

# Short-lived L1 cache for picker metadata (stores serialized JSON to guarantee copy isolation)
_MEMBER_PICKER_CACHE = InMemoryTTLCache(ttl_seconds=30.0, max_size=16)


def invalidate_member_picker_cache() -> None:
    _MEMBER_PICKER_CACHE.invalidate()


def _picker_cache_key(db: Session) -> str:
    bind = db.get_bind() if hasattr(db, "get_bind") else getattr(db, "bind", None)
    if bind is None:
        return "default"
    info = getattr(bind, "info", None)
    if isinstance(info, dict):
        token = info.get("_member_picker_cache_token")
        if not token:
            token = uuid.uuid4().hex
            info["_member_picker_cache_token"] = token
        return token
    return str(id(bind))


def _build_cluster_response(db: Session, cluster: Cluster) -> ClusterResponse:
    m_count = (
        db.query(func.count(ClusterMember.employee_id))
        .join(Employee, Employee.id == ClusterMember.employee_id)
        .filter(
            ClusterMember.cluster_id == cluster.id,
            Employee.status.in_(["active", "on-leave"]),
        )
        .scalar()
        or 0
    )
    a_count = (
        db.query(func.count(Assignment.id))
        .filter_by(cluster_id=cluster.id, is_active=True)
        .scalar()
        or 0
    )
    resp = ClusterResponse.model_validate(cluster)
    resp.dynamic_scopes = cluster.effective_scopes if cluster.is_dynamic else []
    resp.member_count = m_count
    resp.active_assignments_count = a_count
    return resp


@router.post("", response_model=ClusterResponse, status_code=status.HTTP_201_CREATED)
def create_cluster(
    payload: ClusterCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """Admin/HR only: Creates a brand-new static or dynamic smart cluster."""
    existing = (
        db.query(Cluster)
        .filter(func.lower(Cluster.name) == payload.name.lower(), Cluster.is_archived == False)
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail=f"Cluster with name '{payload.name}' already exists.")

    cluster = Cluster(
        name=payload.name,
        description=payload.description,
        dynamic_scope=payload.dynamic_scope,
        dynamic_scopes=payload.dynamic_scopes,
        joined_on_or_after=payload.joined_on_or_after,
        created_by=current_user.id,
    )
    db.add(cluster)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Cluster with name '{payload.name}' already exists.")

    if cluster.is_dynamic:
        populate_cluster_members(db, cluster)
    elif payload.employee_ids:
        req_ids = sorted(set(payload.employee_ids))
        valid_ids = [
            r[0]
            for r in db.query(Employee.id)
            .filter(Employee.id.in_(req_ids), Employee.status.in_(["active", "on-leave"]))
            .all()
        ]
        if valid_ids:
            bind = db.get_bind()
            dialect_name = bind.dialect.name if bind is not None else "sqlite"
            insert_fn = pg_insert if dialect_name == "postgresql" else sqlite_insert
            stmt = (
                insert_fn(ClusterMember)
                .values([{"cluster_id": cluster.id, "employee_id": eid} for eid in valid_ids])
                .on_conflict_do_nothing(index_elements=["cluster_id", "employee_id"])
            )
            db.execute(stmt)
            db.flush()

    audit_service.record(
        db,
        actor=current_user,
        action="cluster.created",
        category="Assessments",
        action_type="Created",
        entity_type="cluster",
        entity_id=cluster.id,
        entity_name=cluster.name,
        summary=f"Created {cluster.dynamic_scope} cluster '{cluster.name}'",
        request=request,
    )

    db.commit()
    db.refresh(cluster)
    return _build_cluster_response(db, cluster)


@router.get("", response_model=List[ClusterResponse])
def list_clusters(
    include_archived: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """Admin/HR only: Lists clusters with active member counts and active assignment counts."""
    query = db.query(Cluster)
    if not include_archived:
        query = query.filter(Cluster.is_archived == False)

    clusters = query.order_by(Cluster.name.asc()).all()
    if not clusters:
        return []

    cluster_ids = [c.id for c in clusters]
    member_counts = dict(
        db.query(ClusterMember.cluster_id, func.count(ClusterMember.employee_id))
        .join(Employee, Employee.id == ClusterMember.employee_id)
        .filter(
            ClusterMember.cluster_id.in_(cluster_ids),
            Employee.status.in_(["active", "on-leave"]),
        )
        .group_by(ClusterMember.cluster_id)
        .all()
    )
    assignment_counts = dict(
        db.query(Assignment.cluster_id, func.count(Assignment.id))
        .filter(Assignment.cluster_id.in_(cluster_ids), Assignment.is_active == True)
        .group_by(Assignment.cluster_id)
        .all()
    )

    results = []
    for c in clusters:
        resp = ClusterResponse.model_validate(c)
        resp.dynamic_scopes = c.effective_scopes if c.is_dynamic else []
        resp.member_count = member_counts.get(c.id, 0)
        resp.active_assignments_count = assignment_counts.get(c.id, 0)
        results.append(resp)

    return results


@router.get("/member-picker-options", response_model=ClusterPickerOptionsResponse)
def get_cluster_member_picker_options(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """
    Single pooled endpoint returning lightweight employee metadata + active DailySheet rosters
    for manual cluster bulk-selection (by DailySheet, Idle status, Skills, Designation, Type).
    Cached per DB bind for 30s as serialized JSON; skips unnecessary MainProject join.
    """
    cache_key = _picker_cache_key(db)
    cached_json = _MEMBER_PICKER_CACHE.get(cache_key)
    if cached_json is not None:
        return ClusterPickerOptionsResponse.model_validate_json(cached_json)

    emp_rows = (
        db.query(
            Employee.id,
            Employee.name,
            Employee.email,
            Employee.designation,
            Employee.employee_type,
            Employee.status,
            Employee.skills,
        )
        .filter(Employee.status.in_(["active", "on-leave"]))
        .order_by(Employee.name.asc())
        .all()
    )

    sheet_rows = (
        db.query(
            DailySheet.id,
            DailySheet.name,
            DailySheet.project_status,
            DailySheet.assigned_employee_ids,
        )
        .filter(
            (DailySheet.project_status.is_(None))
            | (~func.lower(DailySheet.project_status).in_(ARCHIVED_STATUSES))
        )
        .order_by(DailySheet.name.asc())
        .all()
    )

    alloc_rows = (
        db.query(Allocation.employee_id, Allocation.sub_project_id)
        .filter(
            Allocation.is_active == True,
            Allocation.sub_project_id.isnot(None),
        )
        .all()
    )

    eligible_emp_ids = {r[0] for r in emp_rows}
    active_sheet_ids = {r[0] for r in sheet_rows}
    allocated_emp_ids: set[int] = set()
    emp_to_sheets: dict[int, set[int]] = {}
    sheet_to_emps: dict[int, set[int]] = {}

    for emp_id, sheet_id in alloc_rows:
        allocated_emp_ids.add(emp_id)
        if emp_id in eligible_emp_ids and sheet_id in active_sheet_ids:
            emp_to_sheets.setdefault(emp_id, set()).add(sheet_id)
            sheet_to_emps.setdefault(sheet_id, set()).add(emp_id)

    for sheet_id, _name, _st, assigned_ids in sheet_rows:
        if isinstance(assigned_ids, list):
            for raw_id in assigned_ids:
                try:
                    eid = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if eid in eligible_emp_ids:
                    emp_to_sheets.setdefault(eid, set()).add(sheet_id)
                    sheet_to_emps.setdefault(sheet_id, set()).add(eid)

    designations_set: set[str] = set()
    skills_set: set[str] = set()
    emp_types_set: set[str] = set()
    idle_count = 0
    employees_out: List[ClusterPickerEmployee] = []

    for emp_id, name, email, designation, employee_type, emp_status, raw_skills in emp_rows:
        if designation:
            designations_set.add(designation)
        if employee_type:
            emp_types_set.add(employee_type)
        clean_skills: List[str] = []
        if isinstance(raw_skills, list):
            for sk in raw_skills:
                s = str(sk).strip()
                if s:
                    clean_skills.append(s)
                    skills_set.add(s)

        is_idle = (emp_status == "active") and (emp_id not in allocated_emp_ids)
        if is_idle:
            idle_count += 1

        employees_out.append(
            ClusterPickerEmployee(
                id=emp_id,
                name=name,
                email=email,
                designation=designation,
                employee_type=employee_type,
                status=emp_status,
                skills=clean_skills,
                is_idle=is_idle,
                daily_sheet_ids=sorted(emp_to_sheets.get(emp_id, set())),
            )
        )

    sheets_out = [
        ClusterPickerDailySheet(
            id=sheet_id,
            name=sheet_name,
            main_project_name=None,
            project_status=p_status,
            active_member_count=len(sheet_to_emps.get(sheet_id, set())),
        )
        for sheet_id, sheet_name, p_status, _assigned_ids in sheet_rows
    ]

    resp = ClusterPickerOptionsResponse(
        employees=employees_out,
        daily_sheets=sheets_out,
        designations=sorted(designations_set),
        skills=sorted(skills_set),
        employee_types=sorted(emp_types_set),
        idle_count=idle_count,
    )
    _MEMBER_PICKER_CACHE.set(cache_key, resp.model_dump_json())
    return resp


@router.get("/{cluster_id}", response_model=ClusterResponse)
def get_cluster(
    cluster_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """Admin/HR only: Retrieves single cluster details."""
    cluster = db.query(Cluster).filter_by(id=cluster_id).first()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found.")
    return _build_cluster_response(db, cluster)


@router.get("/{cluster_id}/members", response_model=List[ClusterMemberResponse])
def list_cluster_members(
    cluster_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """Admin/HR only: Lists current roster members of a cluster."""
    cluster = db.query(Cluster).filter_by(id=cluster_id).first()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found.")

    rows = (
        db.query(
            ClusterMember.employee_id,
            Employee.name,
            Employee.email,
            Employee.designation,
            Employee.employee_type,
            Employee.status,
            ClusterMember.added_at,
        )
        .join(Employee, Employee.id == ClusterMember.employee_id)
        .filter(
            ClusterMember.cluster_id == cluster_id,
            Employee.status.in_(["active", "on-leave"]),
        )
        .order_by(Employee.name.asc())
        .all()
    )
    return [
        ClusterMemberResponse(
            employee_id=r[0],
            name=r[1],
            email=r[2],
            designation=r[3],
            employee_type=r[4],
            status=r[5],
            added_at=r[6],
        )
        for r in rows
    ]


@router.put("/{cluster_id}", response_model=ClusterResponse)
def update_cluster(
    cluster_id: int,
    payload: ClusterUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """
    Admin/HR only: Updates cluster configuration.
    Archiving must go through DELETE /api/clusters/{cluster_id} to deactivate assignments cleanly.
    Reconciles roster and fans out only when dynamic_scope or joined_on_or_after changes.
    """
    cluster = db.query(Cluster).filter_by(id=cluster_id).with_for_update().first()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found.")
    if cluster.is_archived:
        raise HTTPException(status_code=400, detail="Cannot modify an archived cluster.")

    old_scope = cluster.dynamic_scope
    old_scopes = list(cluster.effective_scopes)
    old_join_date = cluster.joined_on_or_after

    if payload.name and payload.name != cluster.name:
        collision = (
            db.query(Cluster)
            .filter(
                func.lower(Cluster.name) == payload.name.lower(),
                Cluster.is_archived == False,
                Cluster.id != cluster.id,
            )
            .first()
        )
        if collision:
            raise HTTPException(status_code=400, detail="Cluster name already in use.")
        cluster.name = payload.name

    fields_set = payload.model_fields_set
    target_join_date = payload.joined_on_or_after if "joined_on_or_after" in fields_set else cluster.joined_on_or_after

    if "dynamic_scopes" in fields_set or "dynamic_scope" in fields_set:
        incoming_scopes = payload.dynamic_scopes if "dynamic_scopes" in fields_set else None
        incoming_single = payload.dynamic_scope if ("dynamic_scope" in fields_set and payload.dynamic_scope is not None) else cluster.dynamic_scope
        try:
            target_scope, target_scopes_list, target_join_date = _normalize_scope_inputs(
                incoming_single,
                incoming_scopes,
                target_join_date,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    else:
        target_scope = cluster.dynamic_scope
        target_scopes_list = cluster.dynamic_scopes
        if target_scope != "new_joiners" and "new_joiners" not in old_scopes:
            target_join_date = None
        elif ("new_joiners" in old_scopes or target_scope == "new_joiners") and target_join_date is None:
            raise HTTPException(
                status_code=422,
                detail="joined_on_or_after is required when 'new_joiners' is selected.",
            )

    if "description" in fields_set:
        cluster.description = payload.description
    cluster.dynamic_scope = target_scope
    cluster.dynamic_scopes = target_scopes_list
    cluster.joined_on_or_after = target_join_date

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Cluster name already in use.")

    new_scopes = list(cluster.effective_scopes)
    converted_to_custom = (old_scopes != ["custom"]) and (not cluster.is_dynamic)
    scope_changed = (target_scope != old_scope) or (new_scopes != old_scopes) or (target_join_date != old_join_date)
    if scope_changed and cluster.is_dynamic:
        reconcile_dynamic_cluster(db, cluster)

    summary_text = f"Updated cluster '{cluster.name}' (scope={cluster.dynamic_scope})"
    if converted_to_custom:
        summary_text = f"Converted cluster '{cluster.name}' from dynamic ({','.join(old_scopes)}) to custom; preserved existing roster"

    audit_service.record(
        db,
        actor=current_user,
        action="cluster.updated",
        category="Assessments",
        action_type="Updated",
        entity_type="cluster",
        entity_id=cluster.id,
        entity_name=cluster.name,
        summary=summary_text,
        request=request,
    )

    db.commit()
    db.refresh(cluster)
    return _build_cluster_response(db, cluster)


@router.post("/{cluster_id}/members", status_code=status.HTTP_200_OK)
def add_cluster_members(
    cluster_id: int,
    payload: ClusterMemberAdd,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """Admin/HR only: Batch adds eligible employees to a static ('custom') cluster cleanly."""
    cluster = db.query(Cluster).filter_by(id=cluster_id, is_archived=False).first()
    if not cluster:
        raise HTTPException(status_code=404, detail="Target cluster not found or archived.")
    if cluster.is_dynamic:
        raise HTTPException(
            status_code=400,
            detail="Cannot manually add members to a dynamic smart cluster.",
        )

    requested_ids = sorted(set(payload.employee_ids))
    eligible_ids = {
        row[0]
        for row in db.query(Employee.id)
        .filter(
            Employee.id.in_(requested_ids),
            Employee.status.in_(["active", "on-leave"]),
        )
        .all()
    }
    skipped_ids = [eid for eid in requested_ids if eid not in eligible_ids]

    added_count = 0
    if eligible_ids:
        bind = db.get_bind()
        dialect_name = bind.dialect.name if bind is not None else "sqlite"
        insert_fn = pg_insert if dialect_name == "postgresql" else sqlite_insert
        rows_to_insert = [{"cluster_id": cluster.id, "employee_id": eid} for eid in sorted(eligible_ids)]
        stmt = (
            insert_fn(ClusterMember)
            .values(rows_to_insert)
            .on_conflict_do_nothing(index_elements=["cluster_id", "employee_id"])
        )
        res = db.execute(stmt)
        added_count = res.rowcount if hasattr(res, "rowcount") and res.rowcount and res.rowcount > 0 else 0
        db.flush()

    if added_count > 0:
        fan_out_cluster(db, cluster)

    audit_service.record(
        db,
        actor=current_user,
        action="cluster.members_added",
        category="Assessments",
        action_type="Updated",
        entity_type="cluster",
        entity_id=cluster.id,
        entity_name=cluster.name,
        summary=f"Added {added_count} member(s) to cluster '{cluster.name}'",
        request=request,
    )

    db.commit()
    invalidate_member_picker_cache()

    total_members = (
        db.query(func.count(ClusterMember.employee_id))
        .join(Employee, Employee.id == ClusterMember.employee_id)
        .filter(
            ClusterMember.cluster_id == cluster.id,
            Employee.status.in_(["active", "on-leave"]),
        )
        .scalar()
        or 0
    )
    return {
        "detail": f"Successfully added {added_count} member(s).",
        "added_count": added_count,
        "skipped_employee_ids": skipped_ids,
        "total_members": total_members,
        "cluster": _build_cluster_response(db, cluster),
    }


@router.delete("/{cluster_id}/members/{employee_id}", status_code=status.HTTP_200_OK)
def delete_cluster_member(
    cluster_id: int,
    employee_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """Admin/HR only: Removes a member from a static ('custom') cluster with deduplication."""
    cluster = db.query(Cluster).filter_by(id=cluster_id).with_for_update().first()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found.")
    if cluster.is_archived:
        raise HTTPException(status_code=400, detail="Cannot remove members from an archived cluster.")
    if cluster.is_dynamic:
        raise HTTPException(
            status_code=400,
            detail="Cannot manually remove members from a dynamic smart cluster.",
        )

    removed = remove_cluster_member(db, cluster_id, employee_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Cluster member not found.")

    audit_service.record(
        db,
        actor=current_user,
        action="cluster.member_removed",
        category="Assessments",
        action_type="Updated",
        entity_type="cluster",
        entity_id=cluster.id,
        entity_name=cluster.name,
        subject_employee_id=employee_id,
        summary=f"Removed employee #{employee_id} from cluster '{cluster.name}'",
        request=request,
    )

    db.commit()
    invalidate_member_picker_cache()
    return {
        "detail": "Member removed successfully.",
        "cluster": _build_cluster_response(db, cluster),
    }


@router.post("/{cluster_id}/members/remove-bulk", status_code=status.HTTP_200_OK)
def remove_cluster_members_bulk_endpoint(
    cluster_id: int,
    payload: ClusterMemberAdd,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """Admin/HR only: Removes multiple members from a static ('custom') cluster in a single bulk operation."""
    cluster = db.query(Cluster).filter_by(id=cluster_id).with_for_update().first()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found.")
    if cluster.is_archived:
        raise HTTPException(status_code=400, detail="Cannot remove members from an archived cluster.")
    if cluster.is_dynamic:
        raise HTTPException(status_code=400, detail="Cannot manually remove members from a dynamic smart cluster.")

    deleted_count = remove_cluster_members_bulk(db, cluster_id, payload.employee_ids)

    audit_service.record(
        db,
        actor=current_user,
        action="cluster.members_removed_bulk",
        category="Assessments",
        action_type="Updated",
        entity_type="cluster",
        entity_id=cluster.id,
        entity_name=cluster.name,
        summary=f"Bulk removed {deleted_count} member(s) from cluster '{cluster.name}'",
        request=request,
    )

    db.commit()
    invalidate_member_picker_cache()
    return {
        "detail": f"Successfully removed {deleted_count} member(s).",
        "deleted_count": deleted_count,
        "cluster": _build_cluster_response(db, cluster),
    }


@router.delete("/{cluster_id}", status_code=status.HTTP_200_OK)
def delete_cluster(
    cluster_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """Admin/HR only: Soft-archives a cluster and deactivates active assignments with deduplication."""
    cluster = db.query(Cluster).filter_by(id=cluster_id).with_for_update().first()
    if not cluster or cluster.is_archived:
        raise HTTPException(status_code=404, detail="Cluster not found or already archived.")

    archive_cluster(db, cluster_id)

    audit_service.record(
        db,
        actor=current_user,
        action="cluster.archived",
        category="Assessments",
        action_type="Archived",
        entity_type="cluster",
        entity_id=cluster.id,
        entity_name=cluster.name,
        summary=f"Archived cluster '{cluster.name}'",
        request=request,
    )

    db.commit()
    return {"detail": "Cluster archived successfully."}


@router.post("/{cluster_id}/restore", response_model=ClusterResponse, status_code=status.HTTP_200_OK)
def restore_cluster(
    cluster_id: int,
    request: Request,
    payload: Optional[ClusterRestore] = Body(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_assessment_admin),
):
    """
    Admin/HR only: Unarchives a soft-archived cluster while preserving its roster.
    An active cluster name cannot be duplicated; if an active cluster with the same name
    exists, the caller must supply a new unique name in `payload.name`.
    """
    cluster = db.query(Cluster).filter_by(id=cluster_id).with_for_update().first()
    if not cluster:
        raise HTTPException(status_code=404, detail="Cluster not found.")
    if not cluster.is_archived:
        raise HTTPException(status_code=409, detail="Cluster is already active.")

    target_name = (payload.name if payload and payload.name else cluster.name).strip()
    if not target_name:
        raise HTTPException(status_code=422, detail="Cluster name cannot be empty.")

    collision = (
        db.query(Cluster.id)
        .filter(
            func.lower(Cluster.name) == target_name.lower(),
            Cluster.is_archived == False,
            Cluster.id != cluster.id,
        )
        .first()
    )
    if collision:
        raise HTTPException(
            status_code=409,
            detail=(
                f"An active cluster named '{target_name}' already exists. "
                "Please enter a different name to restore this cluster."
            ),
        )

    old_name = cluster.name
    now = datetime.now(timezone.utc)
    cluster.name = target_name
    cluster.is_archived = False
    cluster.updated_at = now

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"An active cluster named '{target_name}' already exists. "
                "Please enter a different name to restore this cluster."
            ),
        )

    if cluster.is_dynamic:
        reconcile_dynamic_cluster(db, cluster)

    reactivated_count = 0
    if payload and payload.restore_assignments:
        reactivated_count = reactivate_assignments_for_cluster(db, cluster.id, now=now)

    rename_note = f" (renamed from '{old_name}')" if target_name != old_name else ""
    asg_note = f" and reactivated {reactivated_count} assignment(s)" if reactivated_count else ""
    audit_service.record(
        db,
        actor=current_user,
        action="cluster.restored",
        category="Assessments",
        action_type="Restored",
        entity_type="cluster",
        entity_id=cluster.id,
        entity_name=cluster.name,
        summary=f"Restored cluster '{cluster.name}'{rename_note}{asg_note}",
        request=request,
    )

    db.commit()
    db.refresh(cluster)
    return _build_cluster_response(db, cluster)
