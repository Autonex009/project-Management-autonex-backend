import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from arq import create_pool
from arq.connections import RedisSettings

import anyio.to_thread

from app.db.database import Base, engine
from app.models import project, allocation, leave, employee, parent_project, user, sub_project, guideline, side_project, skill, notification, wfh, signup_request, referral, payroll, performance_review, perf_eval, onboarding, company_settings, wifi_network, chat, encord_analytics, encord_activity, vendor, audit_log, employee_badge, onboarding_pipeline, employee_document
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
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
from app.observability import setup_metrics
from app.services.scheduler_service import start_scheduler, shutdown_scheduler
from app.api.employee_notes import router as employee_notes_router
from app.api.badges import router as badges_router
from app.api.slack import router as slack_router
from app.api.checkins import router as checkins_router
from app.api.onboarding_pipeline import router as onboarding_pipeline_router
from app.api.employee_documents import router as employee_documents_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- SCHEMA + CATALOG BOOTSTRAP ---
    # These used to run at module import, which meant merely importing this module
    # opened a DB connection and ran DDL — and seed_skills() additionally DELETEs
    # any skill outside ALLOWED_SKILLS. Any tooling that imported the app (alembic
    # env, a REPL, a test collector) silently did both. Running them here means
    # they happen once, on an actual server start, and a transient DB error is
    # logged rather than crashing the import. Set SKIP_DB_BOOTSTRAP=true where the
    # schema is managed solely by migrations.
    if os.getenv("SKIP_DB_BOOTSTRAP", "false").lower() != "true":
        try:
            Base.metadata.create_all(bind=engine)
        except Exception as e:
            logger.error("Schema create_all failed at startup: %s", e)
        try:
            seed_skills()
        except Exception as e:
            logger.error("Skill seeding failed at startup: %s", e)

    # --- REQUEST CONCURRENCY CEILING ---
    # Almost every endpoint here is a sync `def`, so FastAPI runs it on Starlette's
    # anyio threadpool — 40 threads by default. Each one needs a pooled DB
    # connection, but the pool tops out at pool_size + max_overflow (8 on Railway).
    # The surplus threads therefore queue *inside* the connection pool and fail at
    # pool_timeout with a 500. Capping threads near pool capacity moves the queue in
    # front of the handler instead: requests wait, then succeed, rather than
    # burning a 15s timeout each. Tune with WEB_CONCURRENCY_LIMIT.
    try:
        limiter = anyio.to_thread.current_default_thread_limiter()
        configured = os.getenv("WEB_CONCURRENCY_LIMIT")
        if configured:
            limiter.total_tokens = int(configured)
        logger.info("[startup] request thread limit = %s", limiter.total_tokens)
    except Exception as e:
        logger.warning("Could not set request thread limit: %s", e)

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

    # The chat RAG knowledge base is intentionally NOT built here. search_policy()
    # initialises it on first use, so a deployment that does not use the chatbot
    # pays nothing for it at boot — no file reads, no chunking, no embedding call,
    # and no log noise about an unset EMBEDDING_API_KEY. The first policy query
    # builds it on demand.
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


@app.exception_handler(SQLAlchemyTimeoutError)
async def _db_pool_timeout_handler(request: Request, exc: SQLAlchemyTimeoutError):
    """Answer pool-checkout timeouts with 503 instead of an unhandled 500.

    A checkout timeout means the process is saturated, not that the request was
    invalid. Letting it escape produced a ~60-line traceback per request; under
    load that tripped Railway's 500 logs/sec limit and dropped the very lines
    needed to diagnose the incident. 503 + Retry-After also tells the frontend to
    back off rather than retry immediately and deepen the saturation.
    """
    logger.error(
        "DB pool checkout timed out for %s %s — pool saturated",
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=503,
        content={"detail": "Service temporarily busy. Please retry shortly."},
        headers={"Retry-After": "5"},
    )


@app.get("/health", include_in_schema=False)
async def health():
    """Liveness probe. Deliberately touches no database.

    During the pool-exhaustion incident every DB-backed route returned 500, which
    made the service look wholly down. An async, DB-free probe answers a narrower
    question — is the process up and serving? — so saturation can be told apart
    from a crash. Being async, it also does not consume a request worker thread.
    """
    return {"status": "ok"}


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

# Compress responses above ~1KB. The list endpoints return the whole roster —
# measured at ~500KB of JSON for 1000 employees — and several pages fetch it on
# load, so bandwidth and serialization dominate their cost. Measured compression
# on this payload: 3.4x worst case (high-entropy values) up to 50x (uniform).
# Responses below the threshold are passed through untouched, and clients that do
# not send Accept-Encoding: gzip are unaffected, so this changes no API contract.
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Registered last so it wraps the middleware above and times the full request.
# No-ops on Vercel — see app/observability.py.
setup_metrics(app)

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
app.include_router(employee_documents_router)
app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")
