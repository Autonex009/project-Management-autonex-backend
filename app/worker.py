import os
import asyncio
import logging
from datetime import datetime
from typing import Optional
from arq.connections import RedisSettings

# Import your DB and sync service
from app.db.database import SessionLocal
from app.db.database import Base, engine
from app.models import project, allocation, leave, employee, parent_project, user, sub_project, guideline, side_project, skill, notification, wfh, signup_request, referral, payroll, performance_review, perf_eval, onboarding, company_settings, wifi_network, chat, encord_analytics, encord_activity, vendor
from app.services import encord_sync_service

logger = logging.getLogger(__name__)

# Railway provides the Redis connection string via the REDIS_URL environment variable
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

def perform_sync(start: Optional[datetime], end: Optional[datetime]):
    """Synchronous wrapper function to handle the SQLAlchemy session."""
    db = SessionLocal()
    try:
        encord_sync_service.run_sync(db, start=start, end=end)
    finally:
        db.close()

async def run_encord_sync(ctx, start: Optional[datetime], end: Optional[datetime]):
    """
    The async ARQ task. 
    It wraps the synchronous database call in a thread so ARQ's event loop isn't blocked.
    """
    logger.info(f"Worker picked up job. Start: {start} | End: {end}")
    
    # Offload the heavy synchronous work to a background thread
    await asyncio.to_thread(perform_sync, start, end)
    
    logger.info("Worker completed sync job successfully.")

def _perform_user_sync(log_id: str, employee_id: int, start: datetime, end: datetime):
    db = SessionLocal()
    try:
        return encord_sync_service.run_user_sync(db, log_id, employee_id, start, end)
    finally:
        db.close()

async def run_user_sync_task(ctx, log_id: str, employee_id: int, start: datetime, end: datetime):
    logger.info("Worker picked up user Encord sync — employee=%s start=%s end=%s", employee_id, start, end)
    result = await asyncio.to_thread(_perform_user_sync, log_id, employee_id, start, end)
    logger.info("Worker completed user Encord sync: %s", result)
    return result

# ARQ looks for this specific class name when starting up
class WorkerSettings:
    functions = [run_encord_sync, run_user_sync_task]
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
    
    # Optional: Prevent out-of-memory errors on Railway by limiting concurrent jobs
    max_jobs = 2