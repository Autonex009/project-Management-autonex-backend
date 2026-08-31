import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from arq import create_pool
from arq.connections import RedisSettings

from sqlalchemy import inspect, text

from app.db.database import Base, engine
from app.models import project, allocation, leave, employee, parent_project, user, sub_project, guideline, side_project, skill, notification, wfh, signup_request, referral, payroll, performance_review, perf_eval, onboarding, company_settings, wifi_network, chat, encord_analytics, encord_activity, vendor, audit_log, employee_badge, onboarding_pipeline
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.api.projects import router as project_router
from app.api.allocations import router as allocation_router
from app.api.leaves import router as leave_router
from app.api.employees import router as employee_router
from app.api.skills import router as skills_router
from app.api.vendors import router as vendors_router
from app.api.auth import router as auth_router
from app.api.parent_projects import router as parent_projects_router
from app.api.recommendations import router as recommendations_router
from app.api.sub_projects import router as sub_projects_router
from app.api.guidelines import router as guidelines_router
from app.api.side_projects_api import router as side_projects_api_router
from app.api.notifications import router as notifications_router
from app.api.wfh import router as wfh_router
from app.api.signup_requests import router as signup_requests_router
from app.api.referrals import router as referrals_router, external_router as referrals_external_router
from app.api.payroll import router as payroll_router
from app.api.performance_reviews import router as performance_reviews_router
from app.api.perf_evals import router as perf_evals_router
from app.api.onboarding import router as onboarding_router
from app.api.company_settings import router as company_settings_router
from app.api.wifi_networks import router as wifi_networks_router
from app.api.hiring_sync import router as hiring_sync_router
from app.api.chat import router as chat_router
from app.api.encord_sync import router as encord_sync_router
from app.api.analytics import router as analytics_router, me_router as analytics_me_router
from app.api.audit_logs import router as audit_logs_router
from app.seed_skills import seed_skills
from app.services.scheduler_service import start_scheduler, shutdown_scheduler
from app.api.employee_notes import router as employee_notes_router
from app.api.badges import router as badges_router
from app.api.slack import router as slack_router
from app.api.checkins import router as checkins_router
from app.api.onboarding_pipeline import router as onboarding_pipeline_router

Base.metadata.create_all(bind=engine)

logger = logging.getLogger(__name__)


seed_skills()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- ARQ REDIS POOL SETUP ---
    # Only attempt Redis when REDIS_URL is set. On Railway it's injected (background
    # job queue used). On Vercel/serverless (and local dev) it's absent, so we skip
    # entirely — no connection attempt, no cold-start penalty — and /sync falls back
    # to running inline. Wrapped so a transient Redis error never blocks startup.
    app.state.redis_pool = None
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        try:
            app.state.redis_pool = await create_pool(RedisSettings.from_dsn(redis_url))
        except Exception as e:
            logger.warning("ARQ Redis pool unavailable — /sync will run inline: %s", e)

    # Initialize knowledge base for the chat RAG pipeline
    try:
        from app.services.knowledge_service import initialize_knowledge_base
        initialize_knowledge_base()
    except Exception as e:
        logger.warning("Knowledge base init skipped: %s", e)
    try:
        start_scheduler()
    except Exception as e:
        logger.warning("Scheduler start skipped: %s", e)
    yield

    # --- TEARDOWN ---
    try:
        shutdown_scheduler()
    except Exception:
        pass

    # Close ARQ Redis pool gracefully (only if it connected).
    if getattr(app.state, "redis_pool", None):
        try:
            await app.state.redis_pool.close()
        except Exception:
            pass


app = FastAPI(title="Autonex Resource Planning Tool V2", lifespan=lifespan)


if os.environ.get("VERCEL"):
    uploads_dir = Path("/tmp/uploads")
else:
    uploads_dir = Path(__file__).resolve().parents[1] / "uploads"
uploads_dir.mkdir(parents=True, exist_ok=True)

# Configure CORS with an explicit origin allowlist.
# Set CORS_ORIGINS env var as a comma-separated list for production/staging.
# Falls back to common local dev origins when unset.
_default_origins = "http://localhost:3000,http://localhost:5173,http://localhost:8000"
_cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", _default_origins).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(project_router)
app.include_router(allocation_router)
app.include_router(leave_router)
app.include_router(employee_router)
app.include_router(skills_router)
app.include_router(vendors_router) 
app.include_router(auth_router)
app.include_router(parent_projects_router)
app.include_router(recommendations_router)
app.include_router(sub_projects_router)
app.include_router(guidelines_router)
app.include_router(side_projects_api_router)
app.include_router(notifications_router)
app.include_router(wfh_router)
app.include_router(signup_requests_router)
app.include_router(referrals_router)
app.include_router(referrals_external_router)
app.include_router(payroll_router)
app.include_router(performance_reviews_router)
app.include_router(perf_evals_router)
app.include_router(onboarding_router)
app.include_router(company_settings_router)
app.include_router(wifi_networks_router)
app.include_router(hiring_sync_router)
app.include_router(chat_router)
app.include_router(encord_sync_router)
app.include_router(analytics_router)
app.include_router(analytics_me_router)
app.include_router(audit_logs_router)
app.include_router(employee_notes_router)
app.include_router(badges_router)
app.include_router(slack_router)
app.include_router(checkins_router)
app.include_router(onboarding_pipeline_router)
app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")
