"""
Storage Service - Handles uploading and deleting files via Supabase Storage CDN,
with automatic fallback to local disk storage if Supabase credentials are not set.
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
    base_url: str = "",
    upload_dir: Optional[Path] = None,
) -> Tuple[str, Optional[Path]]:
    """
    Uploads a file to Supabase Storage bucket 'guidelines' if configured.
    Falls back to local disk storage if Supabase is not configured.

    Returns:
        (file_url, local_destination_path_or_None)
    """
    if is_supabase_configured():
        bucket = "guidelines"
        url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{stored_name}"
        headers = {
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "apiKey": SUPABASE_KEY,
            "Content-Type": content_type or "application/octet-stream",
        }

        req = urllib.request.Request(url, data=file_bytes, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                pass
        except urllib.error.HTTPError as err:
            error_body = err.read().decode("utf-8", errors="ignore")
            # If bucket not found, create bucket and retry once
            if err.code == 404 or "not found" in error_body.lower():
                _ensure_bucket_exists(bucket)
                req_retry = urllib.request.Request(url, data=file_bytes, headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(req_retry):
                        pass
                    public_url = f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{stored_name}"
                    return public_url, None
                except urllib.error.HTTPError as retry_err:
                    retry_body = retry_err.read().decode("utf-8", errors="ignore")
                    raise RuntimeError(f"Supabase storage upload failed ({retry_err.code}): {retry_body}") from retry_err

            raise RuntimeError(f"Supabase storage upload failed ({err.code}): {error_body}") from err

        public_url = f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{stored_name}"
        return public_url, None


    # Fallback to local disk storage
    if upload_dir is None:
        raise ValueError("upload_dir is required when Supabase storage is not configured")

    destination = upload_dir / stored_name
    destination.write_bytes(file_bytes)
    file_url = f"{base_url.rstrip('/')}/uploads/guidelines/{stored_name}"
    return file_url, destination


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
