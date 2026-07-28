import asyncio
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from arq.jobs import Job, JobStatus

from app.db.database import get_db, SessionLocal
from app.services.auth_service import require_role
from app.services import encord_sync_service

router = APIRouter(prefix="/api/encord", tags=["Encord Sync"], dependencies=[Depends(require_role("admin"))])


class SyncRange(BaseModel):
    date_from: Optional[str] = None   # "YYYY-MM-DD"
    date_to: Optional[str] = None     # "YYYY-MM-DD"


@router.get("/preview")
def preview(db: Session = Depends(get_db)):
    """Read-only: which projects would be synced and the default window. No Encord call."""
    return encord_sync_service.preview(db)


@router.post("/sync", status_code=202)
async def sync(request: Request, payload: Optional[SyncRange] = None):
    """
    Trigger an Encord pull now. Optional date_from/date_to (YYYY-MM-DD) for a backfill;
    otherwise the previous day is pulled.

    Environment-adaptive:
      * Redis available (Railway) -> enqueue a background ARQ job, return a job_id;
        poll /sync/status/{job_id} for progress.
      * No Redis (Vercel serverless / local dev) -> run the sync inline and return the
        result directly (no job_id). The daily scheduled pull is unaffected either way.
    """
    start = end = None
    if payload and payload.date_from:
        try:
            start = datetime.strptime(payload.date_from, "%Y-%m-%d")
            end = (datetime.strptime(payload.date_to, "%Y-%m-%d")
                   if payload.date_to else datetime.now())
        except ValueError:
            raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")

    redis = getattr(request.app.state, "redis_pool", None)

    # No job queue — run inline (synchronously), offloaded to a thread so the event
    # loop isn't blocked. Returns the result directly, no job_id.
    if redis is None:
        def _inline():
            db = SessionLocal()
            try:
                return encord_sync_service.run_sync(db, start=start, end=end)
            finally:
                db.close()
        try:
            result = await asyncio.to_thread(_inline)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        return {"status": "complete", "result": result}

    # Redis available — enqueue a background job. "run_encord_sync" must match the
    # task registered in the ARQ worker (worker.py).
    job = await redis.enqueue_job("run_encord_sync", start, end)
    if not job:
        raise HTTPException(status_code=500, detail="Failed to enqueue sync job in Redis.")
    return {
        "message": "Sync job accepted and enqueued.",
        "job_id": job.job_id,
        "status": "processing",
    }


@router.get("/sync/status/{job_id}")
async def get_sync_status(request: Request, job_id: str):
    """Check the status of a background ARQ job."""
    redis = getattr(request.app.state, "redis_pool", None)
    if redis is None:
        raise HTTPException(status_code=503, detail="Sync queue unavailable (Redis not connected).")

    job = Job(job_id, redis)
    status = await job.status()
    if status == JobStatus.not_found:
        raise HTTPException(status_code=404, detail="Job not found")

    info = await job.info()
    response = {"job_id": job_id, "status": status.value}
    if status == JobStatus.complete and info:
        response["success"] = info.success
        response["result"] = info.result
    return response
