import os
import asyncio
import logging
from datetime import datetime
from typing import Optional
from arq.connections import RedisSettings

# Import your DB and sync service
from app.db.database import SessionLocal
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

# ARQ looks for this specific class name when starting up
class WorkerSettings:
    functions = [run_encord_sync]
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
    
    # Optional: Prevent out-of-memory errors on Railway by limiting concurrent jobs
    max_jobs = 2