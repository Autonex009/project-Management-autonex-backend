# HR Document Vault Implementation Details

This document outlines the complete technical implementation of the HR Document Vault feature, covering database migrations, backend architecture, and frontend UI components.

## 1. Database Schema Changes

### New Table: `employee_documents`
Created to track all HR documents, versions, and their storage locations.
- **id**: Primary Key
- **employee_id**: Foreign Key to `employees`
- **doc_type**: String (e.g., `internship_offer_letter`, `org_policy`)
- **file_url**: String (Storage bucket URL)
- **file_name**: String (Original or generated file name)
- **version**: Integer (Auto-increments per `doc_type` per employee)
- **source**: Enum (`generated`, `uploaded`)
- **uploaded_by**: Foreign Key to `users` (Actor who uploaded/generated)
- **is_active**: Boolean (For soft-deletes and tracking the current version)
- **generated_at**: Timestamp
- **created_at**: Timestamp

### Altered Table: `employees`
- Added **`internship_end_date`** (Date) to track intern conversion timelines and trigger scheduled alerts.
- Added relationship to `employee_documents`.

---

## 2. Backend Implementation (FastAPI)

### New Files Added
- **`app/api/employee_documents.py`**
  - Defines the core API endpoints:
    - `GET /{id}/documents`: List all active documents.
    - `GET /{id}/documents/summary`: Completeness summary (present vs missing).
    - `POST /{id}/documents/upload`: Manual file upload to Supabase.
    - `POST /{id}/documents/generate`: Auto-generate PDF from template.
    - `POST /{id}/documents/bulk-generate`: Bulk generation of all applicable documents.
    - `GET /{id}/documents/{doc_id}/download`: Get signed URL for download.
    - `DELETE /{id}/documents/{doc_id}`: Soft-delete (deactivate) a document.
    - `GET /{id}/documents/{doc_type}/history`: View all versions of a specific document type.
- **`app/models/employee_document.py`**
  - SQLAlchemy model mapping for the `employee_documents` table.
- **`app/schemas/employee_document.py`**
  - Pydantic models for request/response validation. Defines `DOC_TYPE_LABELS` and `DOC_TYPE_APPLICABILITY` rules.
- **`app/services/document_service.py`**
  - Template engine utilizing Jinja2 for HTML templating and ReportLab/xhtml2pdf for PDF generation.
- **`app/services/document_storage_service.py`**
  - Handles Supabase interactions for uploading documents, generating signed URLs, and soft-deleting.

### Modified Files
- **`app/main.py`**
  - Registered the new `employee_documents` router.
- **`app/models/employee.py`**
  - Mapped `internship_end_date` and the `documents` relationship.
- **`app/api/employees.py`**
  - Wired lifecycle triggers: Automatically generates `internship_offer_letter` on intern creation, and `fulltime_offer_letter` on conversion to full-time.
- **`app/services/scheduler_service.py`**
  - Added `_check_internship_endings` APScheduler job to run daily and flag internships ending in 30, 7, or 0 days.
- **`app/services/email_service.py`**
  - Added `send_internship_ending_alert` to notify HR/Admins via Brevo SMTP.
- **`app/services/audit_service.py`**
  - Added a lightweight `log()` shim to record all document operations (upload, generate, delete, download) into the system audit trail without breaking primary flows.

---

## 3. Frontend Implementation (React)

### New Components
- **`src/components/EmployeeDocumentDrawer.jsx`**
  - A slide-in right panel that displays:
    - **Progress Ring Header**: Shows completion ratio (e.g., 4/6 documents present).
    - **Generate All Button**: Triggers the bulk-generate mutation.
    - **Document List**: Iterates through all applicable documents showing status (✅ Present / ⚠️ Missing).
    - **Action Controls**: Upload, Generate (Wand icon), Regenerate (Refresh icon), Download, and Delete.
    - **Version History Panel**: Collapsible sub-panel showing previous versions with direct download links.

### Modified Files
- **`src/services/api.js`**
  - Exported `employeeDocumentApi` mapping to all new backend endpoints (`list`, `summary`, `generate`, `upload`, `download`, `remove`, `bulkGenerate`, `history`).
- **`src/pages/EmployeesPage.jsx`**
  - Added a **Docs** column to the employee roster table.
  - Implemented `DocsSummaryCell` which lazy-fetches the summary API (cached for 60s) to render the `DocProgressBadge`.
  - Added state management to open the `EmployeeDocumentDrawer` when clicking a badge.

---

## 4. Compliance & Operational Polish
- **Audit Logging**: Every action (upload, download, generate, bulk generate, soft-delete) writes an entry to the `audit_logs` table via `audit_service.log()`.
- **Fault Tolerance**: Background operations (like generating documents during employee creation or sending emails) swallow exceptions and log warnings to prevent the primary user action from failing.
- **Secure Access**: Files are not publicly exposed; the system generates short-lived signed URLs for downloading documents.
