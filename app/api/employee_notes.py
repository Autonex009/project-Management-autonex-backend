from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.employee import Employee
from app.models.employee_note import EmployeeNote
from app.models.user import User
from app.schemas.employee_note import (
    EmployeeNoteCreate,
    EmployeeNoteUpdate,
    EmployeeNoteResolve,
    EmployeeNoteResponse,
)
from app.services.auth_service import get_current_user, has_team_read, require_role
from app.services import audit_service

router = APIRouter(
    prefix="/api/employee-notes",
    tags=["Employee Notes"],
    dependencies=[Depends(get_current_user)],
)

ALLOWED_TYPES = {"complaint", "warning", "recognition"}
ALLOWED_SEVERITIES = {"low", "medium", "high"}


def _ensure_note_access_for_employee(employee_id: int, current_user: User) -> None:
    if has_team_read(current_user):
        return
    if current_user.employee_id != employee_id:
        raise HTTPException(status_code=403, detail="Access denied")


def _enrich_note(db: Session, note: EmployeeNote) -> dict:
    employee = db.query(Employee).filter(Employee.id == note.employee_id).first()
    issuer = db.query(User).filter(User.id == note.issued_by).first() if note.issued_by else None

    return {
        "id": note.id,
        "employee_id": note.employee_id,
        "employee_name": employee.name if employee else None,
        "type": note.type,
        "title": note.title,
        "content": note.content,
        "severity": note.severity,
        "status": note.status,
        "issued_by": note.issued_by,
        "issued_by_name": issuer.name if issuer else None,
        "issued_at": note.issued_at,
        "resolved_at": note.resolved_at,
        "resolved_by": note.resolved_by,
        "resolution_note": note.resolution_note,
        "created_at": note.created_at,
        "updated_at": note.updated_at,
    }


@router.post("", response_model=EmployeeNoteResponse)
def create_employee_note(
    payload: EmployeeNoteCreate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm", "hr")),
):
    if payload.type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="Invalid type")

    if payload.severity is not None and payload.severity not in ALLOWED_SEVERITIES:
        raise HTTPException(status_code=400, detail="Invalid severity")

    employee = db.query(Employee).filter(Employee.id == payload.employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    note = EmployeeNote(
        employee_id=payload.employee_id,
        type=payload.type,
        title=payload.title.strip(),
        content=payload.content.strip(),
        severity=payload.severity,
        status="open",
        issued_by=current_user.id,
    )
    db.add(note)
    db.flush()

    audit_service.record(
        db,
        actor=current_user,
        action="employee_note.created",
        category="Employees",
        action_type="Created",
        entity_type="employee_note",
        entity_id=note.id,
        entity_name=note.title,
        subject_employee_id=employee.id,
        subject_name=employee.name,
        details=audit_service.changes(
            audit_service.field_diff("Type", None, note.type),
            audit_service.field_diff("Title", None, note.title),
            audit_service.field_diff("Severity", None, note.severity),
        ),
        summary=f"Added {note.type} for {employee.name}: {note.title}",
        request=http_request,
    )

    db.commit()
    db.refresh(note)
    return _enrich_note(db, note)


@router.get("", response_model=List[dict])
def list_employee_notes(
    employee_id: Optional[int] = None,
    type: Optional[str] = Query(None, alias="type"),
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(EmployeeNote)

    if has_team_read(current_user):
        if employee_id is not None:
            query = query.filter(EmployeeNote.employee_id == employee_id)
    else:
        if not current_user.employee_id:
            raise HTTPException(status_code=403, detail="Access denied")
        query = query.filter(EmployeeNote.employee_id == current_user.employee_id)

    if type:
        if type not in ALLOWED_TYPES:
            raise HTTPException(status_code=400, detail="Invalid type")
        query = query.filter(EmployeeNote.type == type)

    if status:
        query = query.filter(EmployeeNote.status == status)

    notes = query.order_by(EmployeeNote.issued_at.desc(), EmployeeNote.id.desc()).all()
    return [_enrich_note(db, n) for n in notes]


@router.get("/by-employee/{employee_id}", response_model=List[dict])
def list_notes_by_employee(
    employee_id: int,
    type: Optional[str] = Query(None, alias="type"),
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    _ensure_note_access_for_employee(employee_id, current_user)

    query = db.query(EmployeeNote).filter(EmployeeNote.employee_id == employee_id)

    if type:
        if type not in ALLOWED_TYPES:
            raise HTTPException(status_code=400, detail="Invalid type")
        query = query.filter(EmployeeNote.type == type)

    if status:
        query = query.filter(EmployeeNote.status == status)

    notes = query.order_by(EmployeeNote.issued_at.desc(), EmployeeNote.id.desc()).all()
    return [_enrich_note(db, n) for n in notes]


@router.get("/{note_id}", response_model=dict)
def get_employee_note(
    note_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    note = db.query(EmployeeNote).filter(EmployeeNote.id == note_id).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    _ensure_note_access_for_employee(note.employee_id, current_user)
    return _enrich_note(db, note)


@router.put("/{note_id}", response_model=dict)
def update_employee_note(
    note_id: int,
    payload: EmployeeNoteUpdate,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm", "hr")),
):
    note = db.query(EmployeeNote).filter(EmployeeNote.id == note_id).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    if note.status != "open":
        raise HTTPException(status_code=400, detail="Only open notes can be updated")

    if payload.severity is not None and payload.severity not in ALLOWED_SEVERITIES:
        raise HTTPException(status_code=400, detail="Invalid severity")

    update_data = payload.model_dump(exclude_unset=True)
    before = {k: getattr(note, k) for k in update_data.keys()}

    for key, value in update_data.items():
        if key in ("title", "content") and isinstance(value, str):
            value = value.strip()
        setattr(note, key, value)

    employee = db.query(Employee).filter(Employee.id == note.employee_id).first()

    details = audit_service.changes(
        *[
            audit_service.field_diff(key, before.get(key), getattr(note, key))
            for key in update_data.keys()
        ]
    )

    if details:
        audit_service.record(
            db,
            actor=current_user,
            action="employee_note.updated",
            category="Employees",
            action_type="Updated",
            entity_type="employee_note",
            entity_id=note.id,
            entity_name=note.title,
            subject_employee_id=note.employee_id,
            subject_name=employee.name if employee else None,
            details=details,
            summary=f"Updated {note.type} for {employee.name if employee else 'employee'}: {note.title}",
            request=http_request,
        )

    db.commit()
    db.refresh(note)
    return _enrich_note(db, note)


@router.post("/{note_id}/resolve", response_model=dict)
def resolve_employee_note(
    note_id: int,
    payload: EmployeeNoteResolve,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin", "pm", "hr")),
):
    note = db.query(EmployeeNote).filter(EmployeeNote.id == note_id).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    if note.status == "resolved":
        raise HTTPException(status_code=400, detail="Note is already resolved")

    note.status = "resolved"
    note.resolved_at = datetime.now(timezone.utc).replace(tzinfo=None)
    note.resolved_by = current_user.id
    note.resolution_note = (payload.resolution_note or "").strip() or None

    employee = db.query(Employee).filter(Employee.id == note.employee_id).first()

    audit_service.record(
        db,
        actor=current_user,
        action="employee_note.resolved",
        category="Employees",
        action_type="Updated",
        entity_type="employee_note",
        entity_id=note.id,
        entity_name=note.title,
        subject_employee_id=note.employee_id,
        subject_name=employee.name if employee else None,
        details=audit_service.changes(
            audit_service.field_diff("Status", "open", "resolved"),
            audit_service.field_diff("Resolution note", None, note.resolution_note),
        ),
        summary=f"Resolved {note.type} for {employee.name if employee else 'employee'}: {note.title}",
        request=http_request,
    )

    db.commit()
    db.refresh(note)
    return _enrich_note(db, note)


@router.delete("/{note_id}")
def delete_employee_note(
    note_id: int,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    note = db.query(EmployeeNote).filter(EmployeeNote.id == note_id).first()
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")

    employee = db.query(Employee).filter(Employee.id == note.employee_id).first()

    audit_service.record(
        db,
        actor=current_user,
        action="employee_note.deleted",
        category="Employees",
        action_type="Deleted",
        entity_type="employee_note",
        entity_id=note.id,
        entity_name=note.title,
        subject_employee_id=note.employee_id,
        subject_name=employee.name if employee else None,
        details=audit_service.changes(
            audit_service.field_diff("Type", note.type, None),
            audit_service.field_diff("Title", note.title, None),
            audit_service.field_diff("Status", note.status, None),
        ),
        summary=f"Deleted {note.type} for {employee.name if employee else 'employee'}: {note.title}",
        request=http_request,
    )

    db.delete(note)
    db.commit()
    return {"message": "Note deleted"}