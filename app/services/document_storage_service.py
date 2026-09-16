"""HR Document Storage Service — Supabase private bucket helper.

Documents are stored in the private ``employee-documents`` bucket.
Access is always through time-limited signed URLs — never via public links.

Folder convention: /{employee_id}/{doc_type}/{version}.pdf
"""
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY", "")

DOCS_BUCKET = "employee-documents"
SIGNED_URL_EXPIRY_SECONDS = 3600  # 1 hour

# TEMPORARY: Local storage fallback directory
LOCAL_STORAGE_DIR = "local_storage/employee_documents"


def is_supabase_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _auth_headers(content_type: Optional[str] = None) -> dict:
    h = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apiKey": SUPABASE_KEY,
    }
    if content_type:
        h["Content-Type"] = content_type
    return h


def _ensure_private_bucket_exists() -> None:
    """Create the employee-documents bucket if it doesn't exist (private, no public access)."""
    if not is_supabase_configured():
        return
    url = f"{SUPABASE_URL}/storage/v1/bucket"
    payload = json.dumps({
        "id": DOCS_BUCKET,
        "name": DOCS_BUCKET,
        "public": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers=_auth_headers("application/json"), method="POST"
    )
    try:
        with urllib.request.urlopen(req):
            pass
    except Exception:
        # Bucket likely already exists — ignore
        pass


def upload_document(
    employee_id: int,
    doc_type: str,
    version: int,
    file_bytes: bytes,
    content_type: str = "application/pdf",
) -> str:
    """Upload a document PDF to the private bucket.

    Returns the stored_path (e.g. ``42/internship_offer_letter/1.pdf``) which is
    saved in ``employee_documents.file_url``. A signed URL is generated separately
    on download requests.

    Raises RuntimeError when Supabase is not configured or the upload fails.
    """
    stored_path = f"{employee_id}/{doc_type}/{version}.pdf"

    # TEMPORARY: Local storage fallback
    if not is_supabase_configured():
        local_path = os.path.join(LOCAL_STORAGE_DIR, stored_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(file_bytes)
        return stored_path

    url = f"{SUPABASE_URL}/storage/v1/object/{DOCS_BUCKET}/{stored_path}"
    # Use x-upsert so the request works for both new and existing files.
    headers = {**_auth_headers(content_type), "x-upsert": "true"}

    logger.info("[upload_document] Uploading to: %s", url)

    def _put() -> None:
        req = urllib.request.Request(url, data=file_bytes, headers=headers, method="POST")
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            logger.info("[upload_document] Supabase response %s: %s", resp.status, body)

    try:
        _put()
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="ignore")
        logger.error("[upload_document] Supabase HTTP %s: %s", err.code, body)
        if err.code in (404, 400) or "not found" in body.lower() or "bucket" in body.lower():
            _ensure_private_bucket_exists()
            try:
                _put()
            except urllib.error.HTTPError as retry_err:
                rb = retry_err.read().decode("utf-8", errors="ignore")
                raise RuntimeError(
                    f"Document upload to '{DOCS_BUCKET}' failed ({retry_err.code}): {rb}"
                ) from retry_err
        else:
            raise RuntimeError(
                f"Document upload to '{DOCS_BUCKET}' failed ({err.code}): {body}"
            ) from err
    except Exception as exc:
        logger.error("[upload_document] Unexpected error: %s", exc)
        raise RuntimeError(f"Document upload to '{DOCS_BUCKET}' failed: {exc}") from exc

    return stored_path


def get_signed_url(stored_path: str, expires_in: int = SIGNED_URL_EXPIRY_SECONDS) -> Optional[str]:
    """Generate a time-limited signed URL for a private document.

    Returns None if Supabase is not configured or the request fails.
    """
    if not stored_path:
        return None

    # TEMPORARY: Local storage fallback
    if not is_supabase_configured():
        encoded_path = urllib.parse.quote(stored_path)
        return f"http://localhost:8000/api/employees/documents/local/{encoded_path}"

    url = f"{SUPABASE_URL}/storage/v1/object/sign/{DOCS_BUCKET}/{stored_path}"
    payload = json.dumps({"expiresIn": expires_in}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers=_auth_headers("application/json"), method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            signed = data.get("signedURL") or data.get("signedUrl") or ""
            if signed.startswith("/"):
                # Supabase returns signedURL as "/object/sign/..." (no /storage/v1 prefix)
                signed = f"{SUPABASE_URL}/storage/v1{signed}"
            return signed or None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        logger.error("[get_signed_url] Supabase HTTP %s for path '%s': %s", e.code, stored_path, body)
        return None
    except Exception as e:
        logger.error("[get_signed_url] Unexpected error for path '%s': %s", stored_path, e)
        return None


def delete_document(stored_path: str) -> bool:
    """Delete a document from the private bucket. Returns True on success."""
    if not stored_path:
        return False

    # TEMPORARY: Local storage fallback
    if not is_supabase_configured():
        local_path = os.path.join(LOCAL_STORAGE_DIR, stored_path)
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
                return True
            except OSError:
                return False
        return True

    url = f"{SUPABASE_URL}/storage/v1/object/{DOCS_BUCKET}/{stored_path}"
    req = urllib.request.Request(url, headers=_auth_headers(), method="DELETE")
    try:
        with urllib.request.urlopen(req):
            return True
    except Exception:
        return False
