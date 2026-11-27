"""Assessment Proctoring Video Storage Service — Supabase private bucket helper.

Recordings are stored in the private ``assessment-recordings`` bucket with 7-day retention.
Direct-to-bucket browser uploads use short-lived signed upload URLs without upsert overwrites,
and evaluator playback uses short-lived signed read URLs. Local disk fallback is strictly limited
to development/test environments unless ALLOW_LOCAL_RECORDING_FALLBACK=true is explicitly set.
"""
import json
import logging
import os
import urllib.error
import urllib.request
from typing import List, Optional, Set

from app.services.document_storage_service import (
    SUPABASE_URL,
    SUPABASE_KEY,
    STORAGE_TIMEOUT_SECONDS,
    is_supabase_configured,
    _auth_headers,
)
from app.services.project_cache import InMemoryTTLCache

logger = logging.getLogger(__name__)

RECORDINGS_BUCKET = "assessment-recordings"
UPLOAD_SIGNED_URL_EXPIRY_SECONDS = 900   # 15 minutes
PLAY_SIGNED_URL_EXPIRY_SECONDS = 1800    # 30 minutes
LOCAL_RECORDINGS_DIR = "local_storage/assessment_recordings"

# Short-lived L1 RAM cache for signed playback URLs (5-min TTL on 30-min tokens leaves >=25m validity)
_play_url_cache = InMemoryTTLCache(ttl_seconds=300.0, max_size=500)


def is_local_fallback_allowed() -> bool:
    """Rejects silent ephemeral disk fallback in Railway/Vercel production deployments."""
    if os.getenv("ALLOW_LOCAL_RECORDING_FALLBACK", "").lower() == "true":
        return True
    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("VERCEL") or os.getenv("ENV", "").lower() == "production":
        return False
    return True


def _ensure_private_recordings_bucket_exists() -> None:
    """Create the assessment-recordings bucket if it doesn't exist (private)."""
    if not is_supabase_configured():
        return
    url = f"{SUPABASE_URL}/storage/v1/bucket"
    payload = json.dumps({
        "id": RECORDINGS_BUCKET,
        "name": RECORDINGS_BUCKET,
        "public": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers=_auth_headers("application/json"), method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=STORAGE_TIMEOUT_SECONDS):
            pass
    except Exception:
        pass


def _normalize_signed_url(raw_url: str) -> str:
    if raw_url.startswith("/"):
        return f"{SUPABASE_URL}/storage/v1{raw_url}"
    return raw_url


def create_signed_upload_url(
    attempt_id: int,
    stored_path: str,
    expires_in: int = UPLOAD_SIGNED_URL_EXPIRY_SECONDS,
) -> str:
    """Generate a one-shot time-limited signed upload URL (upsert disabled)."""
    if not is_supabase_configured():
        if not is_local_fallback_allowed():
            raise RuntimeError("Supabase recording storage is required in production.")
        return f"/api/assessments/attempts/{attempt_id}/recording/local"

    url = f"{SUPABASE_URL}/storage/v1/object/upload/sign/{RECORDINGS_BUCKET}/{stored_path}"
    headers = _auth_headers("application/json")
    payload = json.dumps({"expiresIn": expires_in}).encode("utf-8")

    def _request() -> str:
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=STORAGE_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read())
            signed = data.get("url") or data.get("signedURL") or data.get("signedUrl") or ""
            if not signed:
                raise RuntimeError("Supabase did not return a signed upload URL.")
            return _normalize_signed_url(signed)

    try:
        return _request()
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="ignore")
        if err.code in (400, 404) or "bucket" in body.lower() or "not found" in body.lower():
            _ensure_private_recordings_bucket_exists()
            try:
                return _request()
            except urllib.error.HTTPError as retry_err:
                rb = retry_err.read().decode("utf-8", errors="ignore")
                raise RuntimeError(
                    f"Signed recording upload URL failed ({retry_err.code}): {rb}"
                ) from retry_err
        raise RuntimeError(f"Signed recording upload URL failed ({err.code}): {body}") from err
    except Exception as exc:
        raise RuntimeError(f"Signed recording upload URL failed: {exc}") from exc


def check_recording_exists(stored_path: str) -> Optional[bool]:
    """
    Verifies whether the recording object physically exists in storage.
    Returns True if present, False if verified absent, None on transient error.
    """
    if not stored_path:
        return False

    if not is_supabase_configured():
        if not is_local_fallback_allowed():
            return None
        local_path = os.path.join(LOCAL_RECORDINGS_DIR, stored_path)
        return os.path.isfile(local_path) and os.path.getsize(local_path) > 0

    url = f"{SUPABASE_URL}/storage/v1/object/authenticated/{RECORDINGS_BUCKET}/{stored_path}"
    headers = {**_auth_headers(), "Range": "bytes=0-0"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=STORAGE_TIMEOUT_SECONDS) as resp:
            return resp.status in (200, 206)
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="ignore").lower()
        if err.code == 404 or (err.code == 400 and ("not_found" in body or "not found" in body)):
            return False
        logger.warning("[check_recording_exists] HTTP %s for '%s': %s", err.code, stored_path, body)
        return None
    except Exception as exc:
        logger.warning("[check_recording_exists] Failed for '%s': %s", stored_path, exc)
        return None


def create_signed_play_url(
    attempt_id: int,
    stored_path: str,
    expires_in: int = PLAY_SIGNED_URL_EXPIRY_SECONDS,
) -> Optional[str]:
    """Generate a time-limited signed download/playback URL for evaluators."""
    if not stored_path:
        return None
    if not is_supabase_configured():
        if not is_local_fallback_allowed():
            return None
        return f"/api/assessments/attempts/{attempt_id}/recording/local"

    cache_key = (SUPABASE_URL, RECORDINGS_BUCKET, stored_path, int(expires_in))
    cache_ttl = min(300.0, max(0.0, float(expires_in) - 1500.0))
    if cache_ttl > 0:
        cached_url = _play_url_cache.get(cache_key)
        if cached_url:
            return cached_url

    url = f"{SUPABASE_URL}/storage/v1/object/sign/{RECORDINGS_BUCKET}/{stored_path}"
    payload = json.dumps({"expiresIn": expires_in}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers=_auth_headers("application/json"), method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=STORAGE_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read())
            signed = data.get("signedURL") or data.get("signedUrl") or ""
            resolved = _normalize_signed_url(signed) if signed else None
            if resolved and cache_ttl > 0:
                _play_url_cache.set(cache_key, resolved, ttl=cache_ttl)
            return resolved
    except Exception as exc:
        logger.error("[create_signed_play_url] Failed for '%s': %s", stored_path, exc)
        return None


def delete_recording_files_bulk(stored_paths: List[str]) -> Set[str]:
    """
    Deletes a batch of proctoring recordings from storage.
    Returns the subset of paths confirmed deleted or explicitly verified absent.
    Never treats blind HTTP 404s as automatic deletion without checking object absence.
    """
    clean_paths = [p for p in stored_paths if p]
    if not clean_paths:
        return set()

    if not is_supabase_configured():
        if not is_local_fallback_allowed():
            return set()
        deleted: Set[str] = set()
        for sp in clean_paths:
            local_path = os.path.join(LOCAL_RECORDINGS_DIR, sp)
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                    deleted.add(sp)
                except OSError as exc:
                    logger.error("[delete_recording_files_bulk] Local delete failed for '%s': %s", local_path, exc)
            else:
                deleted.add(sp)
        return deleted

    url = f"{SUPABASE_URL}/storage/v1/object/{RECORDINGS_BUCKET}"
    payload = json.dumps({"prefixes": clean_paths}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers=_auth_headers("application/json"), method="DELETE"
    )
    confirmed: Set[str] = set()
    try:
        with urllib.request.urlopen(req, timeout=STORAGE_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            try:
                data = json.loads(raw) if raw else []
            except json.JSONDecodeError:
                data = []
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("name"):
                        confirmed.add(item["name"])
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="ignore")
        logger.error("[delete_recording_files_bulk] Supabase HTTP %s: %s", err.code, body)
        if err.code >= 500:
            return set()
    except Exception as exc:
        logger.error("[delete_recording_files_bulk] Unexpected error: %s", exc)
        return set()

    # Fallback existence check only for paths not explicitly returned by /remove
    for sp in clean_paths:
        if sp not in confirmed:
            if check_recording_exists(sp) is False:
                confirmed.add(sp)
    for sp in confirmed:
        _play_url_cache.invalidate((SUPABASE_URL, RECORDINGS_BUCKET, sp, PLAY_SIGNED_URL_EXPIRY_SECONDS))
    return confirmed


def delete_recording_file(stored_path: str) -> bool:
    """Deletes a single proctoring video from storage. Returns True when confirmed deleted or absent."""
    if not stored_path:
        return True
    return stored_path in delete_recording_files_bulk([stored_path])
