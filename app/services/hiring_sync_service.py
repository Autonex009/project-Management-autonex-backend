import json
import logging
import os
import urllib.error
import urllib.request

from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.signup_request import SignupRequest
from app.models.user import User

logger = logging.getLogger(__name__)

HIRING_PORTAL_BASE_URL = os.getenv("HIRING_PORTAL_BASE_URL", "http://127.0.0.1:8001")
HIRING_PORTAL_API_KEY = os.getenv("HIRING_PORTAL_API_KEY", "")

_JOB_TYPE_MAP = {
    "full-time": "Full-time",
    "full time": "Full-time",
    "part-time": "Part-time",
    "part time": "Part-time",
    "intern": "Intern",
    "internship": "Intern",
    "contract": "Contract",
    "contractor": "Contractor",
}

_VALID_DESIGNATIONS = {"Program Manager", "Developer", "QA", "Reviewer", "Annotator"}


def _map_employee_type(job_type: str | None) -> str:
    return _JOB_TYPE_MAP.get((job_type or "").lower(), "Full-time")


def _map_designation(job_title: str | None) -> str:
    return job_title if job_title in _VALID_DESIGNATIONS else "Annotator"


def fetch_hired_candidates() -> list[dict]:
    """Fetch hired candidates from the hiring portal. Raises RuntimeError on failure."""
    url = f"{HIRING_PORTAL_BASE_URL}/api/integrations/pm/hired-candidates"
    req = urllib.request.Request(url)
    if HIRING_PORTAL_API_KEY:
        req.add_header("X-PM-API-Key", HIRING_PORTAL_API_KEY)
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach hiring portal: {exc.reason}") from exc
    except Exception as exc:
        raise RuntimeError(f"Hiring portal error: {exc}") from exc

    if isinstance(body, list):
        return body
    return body.get("data") or body.get("candidates") or []


def run_sync(db: Session) -> dict:
    """
    Pull hired candidates from the hiring portal and create SignupRequests.

    Flow:
      - Fetch all candidates with hr_status == "hired"
      - Skip if already in employees, users, or signup_requests (pending/approved)
      - Create a SignupRequest (status=pending) for each new hire
      - Admin reviews and approves in the Signup Requests panel
      - On approval, existing logic creates Employee + User and sends email credentials

    Raises RuntimeError if the hiring portal cannot be reached.
    """
    candidates = fetch_hired_candidates()

    imported, skipped, errors = [], [], []

    for c in candidates:
        if c.get("hr_status") != "hired":
            continue

        email = c.get("email", "").strip()
        name  = c.get("name", "").strip()

        if not email or not name:
            errors.append({
                "application_id": c.get("application_id"),
                "reason": "Missing name or email",
            })
            continue

        # Skip if already fully onboarded (Employee or User row exists)
        if db.query(Employee).filter(Employee.email == email).first():
            skipped.append({"email": email, "reason": "Already exists as employee"})
            continue
        if db.query(User).filter(User.email == email).first():
            skipped.append({"email": email, "reason": "Already has a user account"})
            continue

        # Skip if a signup request already exists in any status (except rejected, which we delete to allow re-import)
        existing_req = db.query(SignupRequest).filter(SignupRequest.email == email).first()
        if existing_req:
            if existing_req.status == "rejected":
                db.delete(existing_req)
                db.flush()
            else:
                skipped.append({
                    "email": email,
                    "reason": f"Signup request already exists (status: {existing_req.status})",
                })
                continue

        employee_type = _map_employee_type(c.get("job_type"))
        designation   = _map_designation(c.get("job_title"))

        try:
            with db.begin_nested():
                signup_req = SignupRequest(
                    name=name,
                    email=email,
                    phone=c.get("phone"),
                    designation=designation,
                    employee_type=employee_type,
                    skills=[],
                    # reason field used to track this came from the hiring portal
                    reason=f"Auto-imported from hiring portal | Job: {c.get('job_title')} | App ID: {c.get('application_id')}",
                    status="pending",
                )
                db.add(signup_req)

            imported.append({
                "name":           name,
                "email":          email,
                "employee_type":  employee_type,
                "designation":    designation,
                "doc_status":     c.get("doc_status"),
                "application_id": c.get("application_id"),
            })

        except Exception as exc:
            logger.error("[hiring_sync] Failed to create signup request for %s: %s", email, exc)
            errors.append({"email": email, "reason": str(exc)})

    db.commit()

    result = {
        "imported": len(imported),
        "skipped":  len(skipped),
        "errors":   len(errors),
        "details": {
            "imported": imported,
            "skipped":  skipped,
            "errors":   errors,
        },
    }
    logger.info(
        "[hiring_sync] Done — signup_requests created=%s skipped=%s errors=%s",
        result["imported"], result["skipped"], result["errors"],
    )
    return result
