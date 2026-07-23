import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import inspect, text

from app.db.database import Base, engine
from app.models import project, allocation, leave, employee, parent_project, user, sub_project, guideline, side_project, skill, notification, wfh, signup_request, referral, payroll, performance_review, perf_eval, onboarding, company_settings, wifi_network, chat, encord_analytics, encord_activity, vendor
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
from app.api.analytics import router as analytics_router
from app.seed_skills import seed_skills
from app.services.scheduler_service import start_scheduler, shutdown_scheduler

Base.metadata.create_all(bind=engine)

logger = logging.getLogger(__name__)


def sync_main_project_schema() -> None:
    """Backfill missing columns on existing local databases."""
    inspector = inspect(engine)
    try:
        columns = {column["name"] for column in inspector.get_columns("main_projects")}
    except Exception:
        return

    statements = []
    if "project_type" not in columns:
        statements.append("ALTER TABLE main_projects ADD COLUMN project_type TEXT NOT NULL DEFAULT 'Full'")
    if "program_manager_ids" not in columns:
        statements.append("ALTER TABLE main_projects ADD COLUMN program_manager_ids JSON")

    # An organization is just a name + optional PM(s); it no longer requires a
    # start date, so relax the legacy NOT NULL constraint if present.
    if "global_start_date" in columns and engine.dialect.name == "postgresql":
        statements.append("ALTER TABLE main_projects ALTER COLUMN global_start_date DROP NOT NULL")

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))

        # Backfill: seed program_manager_ids from the existing single PM column
        if any("program_manager_ids" in s for s in statements):
            dialect = engine.dialect.name
            if dialect == "postgresql":
                connection.execute(text(
                    "UPDATE main_projects "
                    "SET program_manager_ids = to_jsonb(ARRAY[program_manager_id]) "
                    "WHERE program_manager_id IS NOT NULL"
                ))
            else:
                connection.execute(text(
                    "UPDATE main_projects "
                    "SET program_manager_ids = '[' || program_manager_id || ']' "
                    "WHERE program_manager_id IS NOT NULL"
                ))


sync_main_project_schema()


def sync_encord_analytics_schema() -> None:
    """Add Encord mapping columns to daily_sheets and (re)create the analytics table."""
    inspector = inspect(engine)
    try:
        tables = set(inspector.get_table_names())
    except Exception:
        return

    # daily_sheets: encord_project_hash + sentiment
    try:
        ds_cols = {c["name"] for c in inspector.get_columns("daily_sheets")}
        alters = []
        if "encord_project_hash" not in ds_cols:
            alters.append("ALTER TABLE daily_sheets ADD COLUMN encord_project_hash TEXT")
        if "sentiment" not in ds_cols:
            alters.append("ALTER TABLE daily_sheets ADD COLUMN sentiment TEXT")
        for col in ("annotators_total", "workforce_annotators", "autonex_annotators",
                    "autonex_reviewers", "workforce_reviewers", "qc_count"):
            if col not in ds_cols:
                alters.append(f"ALTER TABLE daily_sheets ADD COLUMN {col} INTEGER DEFAULT 0")
        for col in ("review_time_per_task", "gearing_ratio"):
            if col not in ds_cols:
                alters.append(f"ALTER TABLE daily_sheets ADD COLUMN {col} DOUBLE PRECISION")
        # Workforce is now a list of vendors (JSON) instead of an integer count.
        if "workforce_vendors" not in ds_cols:
            alters.append("ALTER TABLE daily_sheets ADD COLUMN workforce_vendors JSON")
        if "project_types" not in ds_cols:
            alters.append("ALTER TABLE daily_sheets ADD COLUMN project_types JSON")
        if alters:
            with engine.begin() as connection:
                for stmt in alters:
                    connection.execute(text(stmt))
    except Exception:
        pass

    # analytics table: create if missing; recreate if it still has the old
    # main_project_id column (mapping moved to sub-project level; table is disposable).
    if "encord_daily_time_spent" not in tables:
        encord_analytics.Base.metadata.tables["encord_daily_time_spent"].create(bind=engine)
    else:
        try:
            cols = {c["name"] for c in inspector.get_columns("encord_daily_time_spent")}
        except Exception:
            cols = set()
        if "sub_project_id" not in cols:
            with engine.begin() as connection:
                connection.execute(text("DROP TABLE IF EXISTS encord_daily_time_spent"))
            encord_analytics.Base.metadata.tables["encord_daily_time_spent"].create(bind=engine)

    # Per-user daily activity (tasks/labels) table.
    if "encord_daily_activity" not in tables:
        encord_activity.Base.metadata.tables["encord_daily_activity"].create(bind=engine)


sync_encord_analytics_schema()


def sync_leave_schema() -> None:
    """Backfill missing leave columns on existing local and production databases."""
    inspector = inspect(engine)
    try:
        columns = {column["name"] for column in inspector.get_columns("leaves")}
    except Exception:
        return

    statements = []
    dialect = engine.dialect.name

    if "reason" not in columns:
        statements.append("ALTER TABLE leaves ADD COLUMN reason TEXT")
    if "status" not in columns:
        statements.append("ALTER TABLE leaves ADD COLUMN status VARCHAR(50) DEFAULT 'pending'")
    if "approved_by" not in columns:
        statements.append("ALTER TABLE leaves ADD COLUMN approved_by INTEGER")
    if "razorpay_applied" not in columns:
        statements.append("ALTER TABLE leaves ADD COLUMN razorpay_applied BOOLEAN DEFAULT FALSE")
    if "flagged" not in columns:
        statements.append("ALTER TABLE leaves ADD COLUMN flagged BOOLEAN DEFAULT FALSE")
    if "approval_remark" not in columns:
        statements.append("ALTER TABLE leaves ADD COLUMN approval_remark TEXT")
    if "is_half_day" not in columns:
        statements.append("ALTER TABLE leaves ADD COLUMN is_half_day BOOLEAN DEFAULT FALSE")
    if "half_day_slot" not in columns:
        statements.append("ALTER TABLE leaves ADD COLUMN half_day_slot VARCHAR(50)")
    if "created_at" not in columns:
        if dialect == "sqlite":
            statements.append("ALTER TABLE leaves ADD COLUMN created_at TIMESTAMP")
        else:
            statements.append("ALTER TABLE leaves ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    if "updated_at" not in columns:
        if dialect == "sqlite":
            statements.append("ALTER TABLE leaves ADD COLUMN updated_at TIMESTAMP")
        else:
            statements.append("ALTER TABLE leaves ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


sync_leave_schema()


def sync_leave_half_day_schema() -> None:
    """Backfill is_half_day and half_day_slot columns added in dev-staging."""
    inspector = inspect(engine)
    try:
        columns = {column["name"] for column in inspector.get_columns("leaves")}
    except Exception:
        return
    statements = []
    if "is_half_day" not in columns:
        statements.append("ALTER TABLE leaves ADD COLUMN is_half_day BOOLEAN NOT NULL DEFAULT FALSE")
    if "half_day_slot" not in columns:
        statements.append("ALTER TABLE leaves ADD COLUMN half_day_slot TEXT")
    if statements:
        with engine.begin() as connection:
            for stmt in statements:
                connection.execute(text(stmt))


sync_leave_half_day_schema()


def sync_guideline_schema() -> None:
    """Create or backfill the guidelines table on existing databases."""
    inspector = inspect(engine)

    try:
        tables = set(inspector.get_table_names())
    except Exception:
        return

    if "guidelines" not in tables:
        guideline.Base.metadata.tables["guidelines"].create(bind=engine)
        return

    try:
        columns = {column["name"] for column in inspector.get_columns("guidelines")}
    except Exception:
        return

    try:
        foreign_keys = inspector.get_foreign_keys("guidelines")
    except Exception:
        foreign_keys = []

    statements = []
    if "main_project_id" not in columns:
        statements.append("ALTER TABLE guidelines ADD COLUMN main_project_id INTEGER")
    if "sub_project_id" not in columns:
        statements.append("ALTER TABLE guidelines ADD COLUMN sub_project_id INTEGER")
    if "title" not in columns:
        statements.append("ALTER TABLE guidelines ADD COLUMN title TEXT NOT NULL DEFAULT ''")
    if "content" not in columns:
        statements.append("ALTER TABLE guidelines ADD COLUMN content TEXT")
    if "file_name" not in columns:
        statements.append("ALTER TABLE guidelines ADD COLUMN file_name TEXT")
    if "file_url" not in columns:
        statements.append("ALTER TABLE guidelines ADD COLUMN file_url TEXT")
    if "uploaded_by" not in columns:
        statements.append("ALTER TABLE guidelines ADD COLUMN uploaded_by INTEGER")

    dialect = engine.dialect.name
    for foreign_key in foreign_keys:
        constrained_columns = foreign_key.get("constrained_columns") or []
        constraint_name = foreign_key.get("name")
        if constrained_columns == ["sub_project_id"] and constraint_name and dialect == "postgresql":
            statements.append(f'ALTER TABLE guidelines DROP CONSTRAINT IF EXISTS "{constraint_name}"')

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


sync_guideline_schema()


def sync_employee_contact_schema() -> None:
    """Backfill missing employee contact/email/slack columns on existing databases."""
    inspector = inspect(engine)
    try:
        columns = {column["name"] for column in inspector.get_columns("employees")}
    except Exception:
        return

    statements = []
    if "razorpay_email" not in columns:
        statements.append("ALTER TABLE employees ADD COLUMN razorpay_email TEXT")
    if "phone" not in columns:
        statements.append("ALTER TABLE employees ADD COLUMN phone TEXT")
    if "slack_user_id" not in columns:
        statements.append("ALTER TABLE employees ADD COLUMN slack_user_id TEXT")
    if "avatar_url" not in columns:
        statements.append("ALTER TABLE employees ADD COLUMN avatar_url TEXT")

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


sync_employee_contact_schema()


def sync_user_password_reset_schema() -> None:
    """Backfill missing password-reset columns on existing databases."""
    inspector = inspect(engine)
    try:
        columns = {column["name"] for column in inspector.get_columns("users")}
    except Exception:
        return

    statements = []
    if "password_reset_token_hash" not in columns:
        statements.append("ALTER TABLE users ADD COLUMN password_reset_token_hash TEXT")
    if "password_reset_expires_at" not in columns:
        statements.append("ALTER TABLE users ADD COLUMN password_reset_expires_at TIMESTAMP")

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


sync_user_password_reset_schema()


def sync_employee_type_values() -> None:
    """Normalize legacy employee type values stored in existing databases."""
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE employees
                SET employee_type = CASE
                    WHEN employee_type = 'Full-Time' THEN 'Full-time'
                    WHEN employee_type = 'Part-Time' THEN 'Part-time'
                    ELSE employee_type
                END
                WHERE employee_type IN ('Full-Time', 'Part-Time')
                """
            )
        )


sync_employee_type_values()


def sync_employee_salary_schema() -> None:
    """Backfill base_salary column on existing employee tables."""
    inspector = inspect(engine)
    try:
        columns = {column["name"] for column in inspector.get_columns("employees")}
    except Exception:
        return
    if "base_salary" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE employees ADD COLUMN base_salary FLOAT"))


sync_employee_salary_schema()


def sync_salary_encryption_schema() -> None:
    """Add the encrypted salary column and migrate any plaintext salaries into it.

    Adds employees.base_salary_enc, then (only when SALARY_KEY is configured)
    encrypts every existing plaintext base_salary into base_salary_enc and NULLs
    the plaintext. Gating on the key prevents destroying plaintext before it can
    be encrypted — set SALARY_KEY in the production env, then deploy.
    """
    inspector = inspect(engine)
    try:
        columns = {column["name"] for column in inspector.get_columns("employees")}
    except Exception:
        return
    if "base_salary_enc" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE employees ADD COLUMN base_salary_enc TEXT"))

    from app.services.salary_crypto import encryption_enabled, encrypt_salary
    if not encryption_enabled():
        return  # no key yet — leave any plaintext untouched until the key is set

    with engine.begin() as connection:
        rows = connection.execute(text(
            "SELECT id, base_salary FROM employees "
            "WHERE base_salary IS NOT NULL AND (base_salary_enc IS NULL OR base_salary_enc = '')"
        )).fetchall()
        for row in rows:
            enc = encrypt_salary(row.base_salary)
            if enc:
                connection.execute(
                    text("UPDATE employees SET base_salary_enc = :enc, base_salary = NULL WHERE id = :id"),
                    {"enc": enc, "id": row.id},
                )


sync_salary_encryption_schema()


def sync_employee_conversion_schema() -> None:
    """Add intern→full-time conversion audit columns to existing employee tables."""
    inspector = inspect(engine)
    try:
        columns = {column["name"] for column in inspector.get_columns("employees")}
    except Exception:
        return
    statements = []
    if "previous_employee_type" not in columns:
        statements.append("ALTER TABLE employees ADD COLUMN previous_employee_type TEXT")
    if "converted_to_fulltime_at" not in columns:
        statements.append("ALTER TABLE employees ADD COLUMN converted_to_fulltime_at TIMESTAMP")
    if "converted_by" not in columns:
        statements.append("ALTER TABLE employees ADD COLUMN converted_by INTEGER")
    if statements:
        with engine.begin() as connection:
            for stmt in statements:
                connection.execute(text(stmt))


sync_employee_conversion_schema()


def sync_employee_mentor_schema() -> None:
    """Backfill missing employee mentor_id column on existing databases."""
    inspector = inspect(engine)
    try:
        columns = {column["name"] for column in inspector.get_columns("employees")}
    except Exception:
        return
    if "mentor_id" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE employees ADD COLUMN mentor_id INTEGER"))


sync_employee_mentor_schema()


def sync_main_project_annotation_schema() -> None:
    """Add is_annotation column to main_projects if not present."""
    inspector = inspect(engine)
    try:
        columns = {column["name"] for column in inspector.get_columns("main_projects")}
    except Exception:
        return
    if "is_annotation" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE main_projects ADD COLUMN is_annotation BOOLEAN DEFAULT FALSE"))


sync_main_project_annotation_schema()


def sync_daily_sheet_annotation_schema() -> None:
    """Add is_annotation column to daily_sheets if not present."""
    inspector = inspect(engine)
    try:
        columns = {column["name"] for column in inspector.get_columns("daily_sheets")}
    except Exception:
        return
    if "is_annotation" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE daily_sheets ADD COLUMN is_annotation BOOLEAN DEFAULT FALSE"))


sync_daily_sheet_annotation_schema()



def sync_wfh_end_date_schema() -> None:
    """Add end_date column to wfh_requests and backfill existing rows."""
    inspector = inspect(engine)
    try:
        columns = {column["name"] for column in inspector.get_columns("wfh_requests")}
    except Exception:
        return
    if "end_date" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE wfh_requests ADD COLUMN end_date DATE"))
            connection.execute(text("UPDATE wfh_requests SET end_date = wfh_date WHERE end_date IS NULL"))


sync_wfh_end_date_schema()


def sync_daily_sheets_end_date_nullable() -> None:
    """Relax daily_sheets.end_date to allow NULL (sub-projects may be open-ended).

    Idempotent: only runs the ALTER when the column is still NOT NULL. Wrapped in
    try/except so engines that don't support the ALTER (e.g. local SQLite) don't
    block startup — the model definition handles freshly-created tables there.
    """
    inspector = inspect(engine)
    try:
        columns = {c["name"]: c for c in inspector.get_columns("daily_sheets")}
    except Exception:
        return
    end_date_col = columns.get("end_date")
    if end_date_col is None or end_date_col.get("nullable", True):
        return
    try:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE daily_sheets ALTER COLUMN end_date DROP NOT NULL"))
    except Exception:
        pass


sync_daily_sheets_end_date_nullable()


def sync_performance_reviews_schema() -> None:
    """Create the performance_reviews table on existing databases if missing."""
    inspector = inspect(engine)
    try:
        tables = set(inspector.get_table_names())
    except Exception:
        return
    if "performance_reviews" not in tables:
        performance_review.Base.metadata.tables["performance_reviews"].create(bind=engine)


sync_performance_reviews_schema()


def sync_perf_evals_schema() -> None:
    """Create the performance-evaluation tables; backfill new columns on perf_evaluations."""
    inspector = inspect(engine)
    try:
        tables = set(inspector.get_table_names())
    except Exception:
        return

    # Drop the superseded 5-fixed-criteria table (data intentionally discarded).
    if "perf_reviews" in tables:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS perf_reviews"))

    for table_name in ("perf_project_params", "perf_evaluations"):
        if table_name not in tables:
            perf_eval.Base.metadata.tables[table_name].create(bind=engine)

    # Backfill columns added in the star-rating redesign on existing databases.
    if "perf_evaluations" in tables:
        try:
            columns = {column["name"] for column in inspector.get_columns("perf_evaluations")}
        except Exception:
            return
        adds = {
            "overall_comment": "ALTER TABLE perf_evaluations ADD COLUMN overall_comment TEXT",
            "employee_overall_rating": "ALTER TABLE perf_evaluations ADD COLUMN employee_overall_rating REAL",
            "bonus_suggested": "ALTER TABLE perf_evaluations ADD COLUMN bonus_suggested BOOLEAN DEFAULT FALSE",
            "bonus_note": "ALTER TABLE perf_evaluations ADD COLUMN bonus_note TEXT",
        }
        with engine.begin() as connection:
            for col, ddl in adds.items():
                if col not in columns:
                    connection.execute(text(ddl))


sync_perf_evals_schema()


def sync_payroll_schema() -> None:
    """Backfill snapshot columns on existing payroll_leave_adjustments tables."""
    inspector = inspect(engine)
    try:
        tables = set(inspector.get_table_names())
    except Exception:
        return
    if "payroll_leave_adjustments" not in tables:
        return  # create_all() will build it with the columns already present
    try:
        columns = {column["name"] for column in inspector.get_columns("payroll_leave_adjustments")}
    except Exception:
        return
    with engine.begin() as connection:
        if "unpaid_days" not in columns:
            connection.execute(
                text("ALTER TABLE payroll_leave_adjustments ADD COLUMN unpaid_days INTEGER")
            )
        # Specific unpaid dates chosen by the admin (JSON array of ISO date strings).
        if "unpaid_dates" not in columns:
            connection.execute(
                text("ALTER TABLE payroll_leave_adjustments ADD COLUMN unpaid_dates TEXT")
            )


sync_payroll_schema()


def sync_company_settings_schema() -> None:
    """Create the company_settings table on existing databases if missing."""
    inspector = inspect(engine)
    try:
        tables = set(inspector.get_table_names())
    except Exception:
        return
    if "company_settings" not in tables:
        company_settings.Base.metadata.tables["company_settings"].create(bind=engine)

    # Seed default generic settings if missing
    from sqlalchemy.orm import Session as _Session
    with _Session(engine) as session:
        from app.models.company_settings import CompanySetting
        
        default_settings = {
            "office_address": "703, Lodha Supremus\nSaki Vihar Road, Opposite L&T Gate No. 6\nPowai, Mumbai, Maharashtra – 400072\nIndia",
            "google_maps_link": "https://maps.google.com/?q=703+Lodha+Supremus+Saki+Vihar+Road+Powai+Mumbai",
            "company_perks": "Flexible working hours with remote work options\nHealth insurance coverage for employees\nProfessional development & learning budget\nFestival and performance-based bonuses\nPaid national holidays and floater leaves\nWeekend contribution compensation"
        }
        
        for key, value in default_settings.items():
            if session.query(CompanySetting).filter_by(key=key).count() == 0:
                session.add(CompanySetting(key=key, value=value))
        
        session.commit()


def sync_wifi_networks_schema() -> None:
    """Create the wifi_networks table on existing databases if missing."""
    inspector = inspect(engine)
    try:
        tables = set(inspector.get_table_names())
    except Exception:
        return
    if "wifi_networks" not in tables:
        wifi_network.Base.metadata.tables["wifi_networks"].create(bind=engine)

def sync_chat_schema() -> None:
    """Create the chat tables on existing databases if missing."""
    inspector = inspect(engine)
    try:
        tables = set(inspector.get_table_names())
    except Exception:
        return
    if "chat_conversations" not in tables:
        chat.Base.metadata.tables["chat_conversations"].create(bind=engine)
    if "chat_messages" not in tables:
        chat.Base.metadata.tables["chat_messages"].create(bind=engine)

sync_company_settings_schema()
sync_wifi_networks_schema()
sync_chat_schema()
seed_skills()

@asynccontextmanager
async def lifespan(app: FastAPI):
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
    try:
        shutdown_scheduler()
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
app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")
