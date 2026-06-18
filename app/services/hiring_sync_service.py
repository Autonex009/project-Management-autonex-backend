import json
import logging
import os
import secrets
import string
import urllib.error
import urllib.request

from sqlalchemy.orm import Session

from app.api.employees import get_user_role_from_designation
from app.models.employee import Employee
from app.models.user import User
from app.services.auth_service import hash_password
from app.services.email_service import try_send_signup_approved_email

logger = logging.getLogger(__name__)

HIRING_PORTAL_BASE_URL = os.getenv("HIRING_PORTAL_BASE_URL", "http://127.0.0.1:8001")
HIRING_PORTAL_API_KEY = os.getenv("HIRING_PORTAL_API_KEY", "")
PORTAL_URL = os.getenv("PORTAL_URL", "http://localhost:5173")

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


def _gen_temp_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _map_employee_type(job_type: str | None) -> str:
    return _JOB_TYPE_MAP.get((job_type or "").lower(), "Full-time")


def _map_designation(job_title: str | None) -> str:
    return job_title if job_title in _VALID_DESIGNATIONS else "Annotator"


def fetch_hired_candidates() -> list[dict]:
    """Fetch hired candidates from the hiring portal. Raises RuntimeError on failure."""
    url = f"{HIRING_PORTAL_BASE_URL}/api/hr/hired-candidates"
    req = urllib.request.Request(url)
    if HIRING_PORTAL_API_KEY:
        req.add_header("Authorization", f"Bearer {HIRING_PORTAL_API_KEY}")
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
    Pull hired candidates from the hiring portal and create them as employees.

    - Skips candidates already present in the PM portal (matched by email).
    - Creates both an Employee row and a linked User account with a random temp password.
    - Emails credentials to each new hire via Brevo.
    - Uses savepoints so one failure does not roll back successfully imported records.
    - Raises RuntimeError if the hiring portal cannot be reached.
    """
    candidates = fetch_hired_candidates()

    imported, skipped, errors = [], [], []

    for c in candidates:
        if c.get("hr_status") != "hired":
            continue

        email = c.get("email", "").strip()
        name = c.get("name", "").strip()

        if not email or not name:
            errors.append({
                "application_id": c.get("application_id"),
                "reason": "Missing name or email",
            })
            continue

        #  check both tables — Employee row can be deleted while User row persists,
        # which would cause a unique-email constraint violation on User creation.
        employee_exists = db.query(Employee).filter(Employee.email == email).first() is not None
        user_exists = db.query(User).filter(User.email == email).first() is not None
        if employee_exists or user_exists:
            skipped.append({"email": email, "reason": "Already exists in PM portal"})
            continue

        employee_type = _map_employee_type(c.get("job_type"))
        designation = _map_designation(c.get("job_title"))
        temp_password = _gen_temp_password()

        try:
            with db.begin_nested():
                employee = Employee(
                    name=name,
                    email=email,
                    phone=c.get("phone"),
                    employee_type=employee_type,
                    designation=designation,
                    working_hours_per_day=8.0,
                    weekly_availability=40.0,
                    skills=[],
                    productivity_baseline=1.0,
                    status="active",
                )
                db.add(employee)
                db.flush()

                user = User(
                    email=email,
                    password_hash=hash_password(temp_password),
                    name=name,
                    role=get_user_role_from_designation(designation),
                    employee_id=employee.id,
                    skills=[],
                    is_active=True,
                )
                db.add(user)
                db.flush()

            email_sent = try_send_signup_approved_email(
                to_email=email,
                to_name=name,
                temp_password=temp_password,
                portal_url=PORTAL_URL,
            )

            imported.append({
                "name": name,
                "email": email,
                "employee_type": employee_type,
                "designation": designation,
                "doc_status": c.get("doc_status"),
                "application_id": c.get("application_id"),
                "email_sent": email_sent,
            })

        except Exception as exc:
            logger.error("[hiring_sync] Failed to import %s: %s", email, exc)
            errors.append({"email": email, "reason": str(exc)})

    db.commit()

    result = {
        "imported": len(imported),
        "skipped": len(skipped),
        "errors": len(errors),
        "details": {
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
        },
    }
    logger.info(
        "[hiring_sync] Done — imported=%s skipped=%s errors=%s",
        result["imported"], result["skipped"], result["errors"],
    )
    return result
