from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from arq.jobs import Job, JobStatus

from app.db.database import get_db
from app.services.auth_service import require_role
from app.services import encord_sync_service
from arq.jobs import Job, JobStatus

router = APIRouter(prefix="/api/encord", tags=["Encord Sync"], dependencies=[Depends(require_role("admin"))])


class SyncRange(BaseModel):
    date_from: Optional[str] = None   # "YYYY-MM-DD"
    date_to: Optional[str] = None     # "YYYY-MM-DD"


@router.get("/preview")
def preview(db: Session = Depends(get_db)):
    """Read-only: which projects would be synced and the default window. No Encord call."""
    return encord_sync_service.preview(db)


# @router.post("/sync")
# def sync(payload: Optional[SyncRange] = None, db: Session = Depends(get_db)):
#     """
#     Trigger an Encord pull now. Optional date_from/date_to (YYYY-MM-DD) for a backfill;
#     otherwise the previous day is pulled. The same logic runs daily via the scheduler.
#     """
#     start = end = None
#     if payload and payload.date_from:
#         try:
#             start = datetime.strptime(payload.date_from, "%Y-%m-%d")
#             end = (datetime.strptime(payload.date_to, "%Y-%m-%d")
#                    if payload.date_to else datetime.now())
#         except ValueError:
#             raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
#     try:
#         return encord_sync_service.run_sync(db, start=start, end=end)
#     except RuntimeError as exc:
#         raise HTTPException(status_code=500, detail=str(exc))

# 1. Add status_code=202 (Accepted)
@router.post("/sync", status_code=202)
async def sync(request: Request, payload: Optional[SyncRange] = None):
    """
    Validates input and pushes the sync job to the Redis queue.
    """
    start = end = None
    if payload and payload.date_from:
        try:
            start = datetime.strptime(payload.date_from, "%Y-%m-%d")
            end = (datetime.strptime(payload.date_to, "%Y-%m-%d")
                   if payload.date_to else datetime.now())
        except ValueError:
            raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD")
            
    # 2. Access the Redis pool attached to the app state in main.py
    redis = request.app.state.redis_pool
    
    # 3. Enqueue the job. The string "run_encord_sync" MUST match the async function name in worker.py
    job = await redis.enqueue_job("run_encord_sync", start, end)
    
    if not job:
        raise HTTPException(status_code=500, detail="Failed to enqueue sync job in Redis.")
    
    # 4. Return immediately while the worker handles the heavy lifting
    return {
        "message": "Sync job accepted and enqueued.", 
        "job_id": job.job_id,
        "status": "processing"
    }

@router.get("/sync/status/{job_id}")
async def get_sync_status(request: Request, job_id: str):
    """Check the status of a background ARQ job."""
    redis = request.app.state.redis_pool
    job = Job(job_id, redis)
    
    # Get current status (queued, in_progress, complete, not_found)
    status = await job.status()
    
    if status == JobStatus.not_found:
        raise HTTPException(status_code=404, detail="Job not found")
        
    # Get job execution info (result, errors, etc.)
    info = await job.info()
    
    response = {
        "job_id": job_id,
        "status": status.value,
    }
    
    # If the job is done, attach the final result or error
    if status == JobStatus.complete and info:
        response["success"] = info.success
        response["result"] = info.result  # This will be whatever your worker returns
        
    return response
