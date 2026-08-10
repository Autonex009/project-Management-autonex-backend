"""Uploading and deleting files via Supabase Storage.

Every new upload goes to Supabase — avatars and guideline documents alike. There is no
disk fallback: the host filesystem is ephemeral on both Railway and Vercel, so a locally
written file is gone by the next deploy while the row still points at it. The document
then 404s, which reads as the database having lost it.

Deletion still understands local URLs, because rows written before this change may point
at `/uploads/...`; those files stay readable and removable, they just cannot be created
any more.
"""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY", "")


def is_supabase_configured() -> bool:
    """Return True if Supabase storage credentials are available in environment."""
    return bool(SUPABASE_URL and SUPABASE_KEY)


AVATAR_BUCKET = "avatars"


def _public_url(bucket: str, stored_name: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{stored_name}"


def upload_to_bucket(
    bucket: str,
    file_bytes: bytes,
    stored_name: str,
    content_type: str = "application/octet-stream",
) -> str:
    """Upload bytes to a public Supabase bucket and return the public URL.

    Raises RuntimeError when Supabase is not configured. That is deliberate and is
    the difference from `upload_guideline_file`: callers that must NOT silently
    fall back to local disk (avatars) rely on this to fail loudly. Disk-backed
    uploads do not survive a redeploy on Railway or Vercel — the row keeps a URL
    while the file disappears, which reads as "the database lost my picture".
    """
    if not is_supabase_configured():
        raise RuntimeError(
            "Supabase storage is not configured — set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY"
        )

    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{stored_name}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apiKey": SUPABASE_KEY,
        "Content-Type": content_type or "application/octet-stream",
    }

    def _put() -> None:
        req = urllib.request.Request(url, data=file_bytes, headers=headers, method="POST")
        with urllib.request.urlopen(req):
            pass

    try:
        _put()
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="ignore")
        # A missing bucket is recoverable: create it and retry once, so a fresh
        # environment works without anyone clicking through the Supabase console.
        if err.code == 404 or "not found" in body.lower():
            _ensure_bucket_exists(bucket)
            try:
                _put()
            except urllib.error.HTTPError as retry_err:
                retry_body = retry_err.read().decode("utf-8", errors="ignore")
                raise RuntimeError(
                    f"Supabase upload to '{bucket}' failed ({retry_err.code}): {retry_body}"
                ) from retry_err
        else:
            raise RuntimeError(
                f"Supabase upload to '{bucket}' failed ({err.code}): {body}"
            ) from err

    return _public_url(bucket, stored_name)


def delete_from_bucket(bucket: str, file_url: str) -> bool:
    """Delete an object addressed by its public URL. False if it is not ours.

    Tolerant by design — it is called with whatever the row happened to hold,
    including legacy `/uploads/avatars/...` URLs from the disk-backed era, which
    simply do not match and are left alone.
    """
    if not file_url or not is_supabase_configured():
        return False

    prefix = _public_url(bucket, "")
    if not file_url.startswith(prefix):
        return False

    stored_name = file_url[len(prefix):].split("?", 1)[0]
    if not stored_name:
        return False

    req = urllib.request.Request(
        f"{SUPABASE_URL}/storage/v1/object/{bucket}/{stored_name}",
        headers={"Authorization": f"Bearer {SUPABASE_KEY}", "apiKey": SUPABASE_KEY},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req):
            return True
    except Exception:
        return False


def _ensure_bucket_exists(bucket: str = "guidelines") -> None:
    """Attempts to create the public storage bucket in Supabase if it doesn't already exist."""
    if not is_supabase_configured():
        return
    url = f"{SUPABASE_URL}/storage/v1/bucket"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apiKey": SUPABASE_KEY,
        "Content-Type": "application/json",
    }
    payload = json.dumps({"id": bucket, "name": bucket, "public": True}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req):
            pass
    except Exception:
        # Ignore if bucket already exists or error occurs
        pass


def upload_guideline_file(
    file_bytes: bytes,
    stored_name: str,
    content_type: str = "application/octet-stream",
) -> str:
    """Upload a document to the public 'guidelines' bucket and return its public URL.

    Raises RuntimeError when Supabase is unreachable or unconfigured, rather than writing
    to disk — see the module docstring for why a disk copy is worse than a failed upload.
    """
    if not is_supabase_configured():
        raise RuntimeError(
            "Supabase storage is not configured — set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY"
        )

    bucket = "guidelines"
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{stored_name}"
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apiKey": SUPABASE_KEY,
        "Content-Type": content_type or "application/octet-stream",
    }

    def _put() -> None:
        req = urllib.request.Request(url, data=file_bytes, headers=headers, method="POST")
        with urllib.request.urlopen(req):
            pass

    try:
        _put()
    except urllib.error.HTTPError as err:
        error_body = err.read().decode("utf-8", errors="ignore")
        # A missing bucket is recoverable: create it and retry once. Anything else is not.
        if err.code == 404 or "not found" in error_body.lower():
            _ensure_bucket_exists(bucket)
            try:
                _put()
            except urllib.error.HTTPError as retry_err:
                retry_body = retry_err.read().decode("utf-8", errors="ignore")
                raise RuntimeError(
                    f"Supabase storage upload failed ({retry_err.code}): {retry_body}"
                ) from retry_err
        else:
            raise RuntimeError(
                f"Supabase storage upload failed ({err.code}): {error_body}"
            ) from err

    return _public_url(bucket, stored_name)


def delete_guideline_file(file_url: str, upload_dir: Optional[Path] = None) -> bool:
    """
    Deletes a guideline file from Supabase Storage or local disk depending on file_url.
    """
    if not file_url:
        return False

    # Check if URL belongs to Supabase Storage
    if SUPABASE_URL and file_url.startswith(f"{SUPABASE_URL}/storage/v1/object/public/guidelines/"):
        stored_name = file_url.rsplit("/", 1)[-1]
        bucket = "guidelines"
        url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{stored_name}"
        headers = {
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "apiKey": SUPABASE_KEY,
        }
        req = urllib.request.Request(url, headers=headers, method="DELETE")
        try:
            with urllib.request.urlopen(req):
                return True
        except Exception:
            return False

    # Fallback / Local disk deletion
    if "/uploads/guidelines/" in file_url and upload_dir:
        stored_name = file_url.rsplit("/", 1)[-1]
        stored_file = upload_dir / stored_name
        if stored_file.exists():
            stored_file.unlink()
            return True

    return False
