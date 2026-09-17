"""HR Employee Documents API.

Endpoints for listing, uploading (manual), generating (template-based),
downloading, and deleting employee documents.
"""
import io
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.employee import Employee
from app.models.employee_document import EmployeeDocument
from app.models.user import User
from app.schemas.employee_document import (
    EmployeeDocumentResponse,
    EmployeeDocumentsListResponse,
    GenerateDocumentRequest,
    DOC_TYPE_LABELS,
    DOC_TYPE_APPLICABILITY,
)
from app.services.auth_service import get_current_user, require_role
from app.services import audit_service
from app.services.document_storage_service import (
    upload_document,
    get_signed_url,
    delete_document,
    is_supabase_configured,
)
from app.services.document_service import generate_document

router = APIRouter(
    prefix="/api/employees",
    tags=["Employee Documents"],
    dependencies=[Depends(require_role("hr", "admin"))],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

# TEMPORARY: Local storage fallback endpoint
import os
from app.services.document_storage_service import LOCAL_STORAGE_DIR

@router.get("/documents/local/{file_path:path}", dependencies=[])
def download_local_document(file_path: str):
    """Serve a document from the local disk when Supabase is not configured."""
    # Ensure no directory traversal
    if ".." in file_path or file_path.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid file path")
        
    full_path = os.path.join(LOCAL_STORAGE_DIR, file_path)
    if not os.path.exists(full_path) or not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="File not found")
        
    return FileResponse(full_path, media_type="application/pdf")

def _enrich_doc(doc: EmployeeDocument, db: Session) -> dict:
    uploader_name = None
    if doc.uploaded_by:
        u = db.query(User).filter(User.id == doc.uploaded_by).first()
        if u:
            uploader_name = u.name or u.email

    download_url = None
    if doc.file_url:
        download_url = get_signed_url(doc.file_url)

    return {
        "id": doc.id,
        "employee_id": doc.employee_id,
        "doc_type": doc.doc_type,
        "doc_type_label": DOC_TYPE_LABELS.get(doc.doc_type, doc.doc_type),
        "file_url": doc.file_url,
        "file_name": doc.file_name,
        "version": doc.version,
        "source": doc.source,
        "uploaded_by": doc.uploaded_by,
        "uploaded_by_name": uploader_name,
        "is_active": doc.is_active,
        "generated_at": doc.generated_at,
        "created_at": doc.created_at,
        "download_url": download_url,
    }


def _get_employee_or_404(employee_id: int, db: Session) -> Employee:
    emp = db.query(Employee).filter(Employee.id == employee_id).first()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    return emp


# ── GET /api/employees/{id}/documents ─────────────────────────────────────────

@router.get("/{employee_id}/documents")
def list_employee_documents(
    employee_id: int,
    include_inactive: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all documents for an employee (active versions only by default)."""
    pass  # auth enforced by dependency
    emp = _get_employee_or_404(employee_id, db)

    q = db.query(EmployeeDocument).filter(EmployeeDocument.employee_id == employee_id)
    if not include_inactive:
        q = q.filter(EmployeeDocument.is_active == True)
    docs = q.order_by(EmployeeDocument.doc_type, EmployeeDocument.version.desc()).all()

    enriched = [_enrich_doc(d, db) for d in docs]

    # Build completeness summary
    applicable = DOC_TYPE_APPLICABILITY.get(emp.employee_type, [])
    present_types = {d.doc_type for d in docs if d.is_active}
    missing = [t for t in applicable if t not in present_types]

    return {
        "employee_id": employee_id,
        "employee_name": emp.name,
        "employee_type": emp.employee_type,
        "total": len(enriched),
        "applicable_count": len(applicable),
        "present_count": len(present_types),
        "missing_doc_types": missing,
        "documents": enriched,
    }


# ── POST /api/employees/{id}/documents/upload ─────────────────────────────────

@router.post("/{employee_id}/documents/upload")
async def upload_employee_document(
    employee_id: int,
    doc_type: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually upload a PDF document for an employee."""
    pass  # auth enforced by dependency

    VALID_DOC_TYPES = {
        "internship_offer_letter", "fulltime_offer_letter",
        "internship_completion_certificate", "experience_letter",
        "salary_structure", "org_policy",
    }
    if doc_type not in VALID_DOC_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid doc_type: {doc_type}")

    _get_employee_or_404(employee_id, db)

    # Determine next version number
    latest = (
        db.query(EmployeeDocument)
        .filter(
            EmployeeDocument.employee_id == employee_id,
            EmployeeDocument.doc_type == doc_type,
        )
        .order_by(EmployeeDocument.version.desc())
        .first()
    )
    next_version = (latest.version + 1) if latest else 1

    # Read file bytes
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    content_type = file.content_type or "application/pdf"
    stored_path: Optional[str] = None

    # TEMPORARY: Local storage fallback unconditionally calls upload_document
    try:
        stored_path = upload_document(
            employee_id=employee_id,
            doc_type=doc_type,
            version=next_version,
            file_bytes=file_bytes,
            content_type=content_type,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Deactivate any previous active version of this doc type
    db.query(EmployeeDocument).filter(
        EmployeeDocument.employee_id == employee_id,
        EmployeeDocument.doc_type == doc_type,
        EmployeeDocument.is_active == True,
    ).update({"is_active": False})

    doc = EmployeeDocument(
        employee_id=employee_id,
        doc_type=doc_type,
        file_url=stored_path,
        file_name=file.filename,
        version=next_version,
        source="uploaded",
        uploaded_by=current_user.id,
        is_active=True,
        generated_at=datetime.now(timezone.utc),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    audit_service.log(
        db=db,
        actor_id=current_user.id,
        action="document_uploaded",
        target_type="employee_document",
        target_id=doc.id,
        metadata={
            "employee_id": employee_id,
            "doc_type": doc_type,
            "version": next_version,
            "file_name": file.filename,
        },
    )

    return _enrich_doc(doc, db)


# ── GET /api/employees/{id}/documents/{doc_id}/download ───────────────────────

@router.get("/{employee_id}/documents/{doc_id}/download")
def download_employee_document(
    employee_id: int,
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return a time-limited signed URL to download a document."""
    pass  # auth enforced by dependency

    doc = (
        db.query(EmployeeDocument)
        .filter(
            EmployeeDocument.id == doc_id,
            EmployeeDocument.employee_id == employee_id,
        )
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if not doc.file_url:
        raise HTTPException(status_code=404, detail="No file stored for this document")

    signed_url = get_signed_url(doc.file_url)
    if not signed_url:
        raise HTTPException(
            status_code=503,
            detail="Could not generate download URL — Supabase may not be configured",
        )

    audit_service.log(
        db=db,
        actor_id=current_user.id,
        action="document_downloaded",
        target_type="employee_document",
        target_id=doc_id,
        metadata={"employee_id": employee_id, "doc_type": doc.doc_type},
    )

    return {"download_url": signed_url, "expires_in_seconds": 3600, "file_name": doc.file_name}


# ── DELETE /api/employees/{id}/documents/{doc_id} ─────────────────────────────

@router.delete("/{employee_id}/documents/{doc_id}")
def delete_employee_document(
    employee_id: int,
    doc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Soft-delete (deactivate) a document. HR-only."""
    pass  # auth enforced by dependency

    doc = (
        db.query(EmployeeDocument)
        .filter(
            EmployeeDocument.id == doc_id,
            EmployeeDocument.employee_id == employee_id,
        )
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Delete from Supabase storage (best-effort — log but don't block if it fails)
    if doc.file_url:
        deleted = delete_document(doc.file_url)
        if not deleted:
            import logging
            logging.getLogger(__name__).warning(
                "[delete_employee_document] Failed to delete '%s' from Supabase storage",
                doc.file_url,
            )

    doc.is_active = False
    db.commit()

    audit_service.log(
        db=db,
        actor_id=current_user.id,
        action="document_deleted",
        target_type="employee_document",
        target_id=doc_id,
        metadata={"employee_id": employee_id, "doc_type": doc.doc_type},
    )

    return {"detail": "Document deactivated", "doc_id": doc_id}

@router.get("/test-delete-debug")
def test_delete_debug(path: str):
    """Temporary endpoint to test Supabase deletion and see the exact response on screen."""
    import urllib.request
    import json
    import os
    
    SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
    SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY", "")
    DOCS_BUCKET = "employee-documents"
    
    url = f"{SUPABASE_URL}/storage/v1/object/remove/{DOCS_BUCKET}"
    payload = json.dumps({"prefixes": [path]}).encode("utf-8")
    
    req = urllib.request.Request(url, data=payload, headers={
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apiKey": SUPABASE_KEY,
        "Content-Type": "application/json"
    }, method="POST")
    
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return {"status": resp.status, "body": body, "key_used": SUPABASE_KEY[:10] + "..." if SUPABASE_KEY else "NONE"}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error_body": e.read().decode("utf-8", errors="ignore")}
    except Exception as e:
        return {"error": str(e)}

# ── GET /api/employees/{id}/documents/summary ─────────────────────────────────

@router.get("/{employee_id}/documents/summary")
def employee_document_summary(
    employee_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return a compact completeness summary for the employee (for the table badge)."""
    pass  # auth enforced by dependency
    emp = _get_employee_or_404(employee_id, db)

    docs = (
        db.query(EmployeeDocument)
        .filter(
            EmployeeDocument.employee_id == employee_id,
            EmployeeDocument.is_active == True,
        )
        .all()
    )
    present_types = {d.doc_type for d in docs}
    applicable = DOC_TYPE_APPLICABILITY.get(emp.employee_type, [])
    missing = [t for t in applicable if t not in present_types]

    return {
        "employee_id": employee_id,
        "applicable": len(applicable),
        "present": len(present_types),
        "missing": missing,
        "complete": len(missing) == 0,
    }


# ── POST /api/employees/{id}/documents/generate ───────────────────────────────

@router.post("/{employee_id}/documents/generate")
def generate_employee_document(
    employee_id: int,
    payload: GenerateDocumentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Auto-generate a PDF document from a template for the given employee."""
    doc_type = payload.doc_type
    VALID_DOC_TYPES = {
        "internship_offer_letter", "fulltime_offer_letter",
        "internship_completion_certificate", "experience_letter",
        "salary_structure",
    }
    if doc_type not in VALID_DOC_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"doc_type '{doc_type}' cannot be auto-generated. "
                   "Use the upload endpoint for org_policy or unsupported types.",
        )

    _get_employee_or_404(employee_id, db)

    try:
        doc = generate_document(
            employee_id=employee_id,
            doc_type=doc_type,
            db=db,
            uploaded_by=current_user.id,
            dynamic_data=payload.dynamic_data,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    audit_service.log(
        db=db,
        actor_id=current_user.id,
        action="document_generated",
        target_type="employee_document",
        target_id=doc.id,
        metadata={
            "employee_id": employee_id,
            "doc_type": doc_type,
            "version": doc.version,
        },
    )

    return _enrich_doc(doc, db)


# ── GET /api/employees/{id}/documents/{doc_type}/history ─────────────────────

@router.get("/{employee_id}/documents/{doc_type}/history")
def employee_document_history(
    employee_id: int,
    doc_type: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return all versions (active and inactive) of a single doc type for an employee.

    Used by the version history panel in the Document Drawer — e.g. to show
    all uploaded / generated revisions of the org_policy document.
    """
    _get_employee_or_404(employee_id, db)

    docs = (
        db.query(EmployeeDocument)
        .filter(
            EmployeeDocument.employee_id == employee_id,
            EmployeeDocument.doc_type == doc_type,
        )
        .order_by(EmployeeDocument.version.desc())
        .all()
    )

    return [_enrich_doc(d, db) for d in docs]


# ── POST /api/employees/{id}/documents/bulk-generate ─────────────────────────

@router.post("/{employee_id}/documents/bulk-generate")
def bulk_generate_employee_documents(
    employee_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate (or regenerate) all auto-generatable documents for an employee.

    Skips org_policy (must be uploaded manually). Returns per-doc results so the
    frontend can show which succeeded and which failed without stopping on the first
    error.
    """
    _get_employee_or_404(employee_id, db)

    GENERATABLE = [
        "internship_offer_letter",
        "fulltime_offer_letter",
        "internship_completion_certificate",
        "experience_letter",
        "salary_structure",
    ]

    results = []
    for doc_type in GENERATABLE:
        try:
            doc = generate_document(
                employee_id=employee_id,
                doc_type=doc_type,
                db=db,
                uploaded_by=current_user.id,
            )
            audit_service.log(
                db=db,
                actor_id=current_user.id,
                action="document_generated",
                target_type="employee_document",
                target_id=doc.id,
                metadata={
                    "employee_id": employee_id,
                    "doc_type": doc_type,
                    "version": doc.version,
                    "bulk": True,
                },
            )
            results.append({"doc_type": doc_type, "status": "ok", "version": doc.version})
        except Exception as exc:
            results.append({"doc_type": doc_type, "status": "error", "detail": str(exc)})

    return {"employee_id": employee_id, "results": results}
