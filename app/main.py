import os
from pathlib import Path

from sqlalchemy import inspect, text

from app.db.database import Base, engine
from app.models import project, allocation, leave, employee, parent_project, user, sub_project, guideline, side_project, skill, notification, wfh, signup_request, referral, payroll, performance_review
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.api.projects import router as project_router
from app.api.allocations import router as allocation_router
from app.api.leaves import router as leave_router
from app.api.employees import router as employee_router
from app.api.skills import router as skills_router
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
from app.seed_skills import seed_skills

Base.metadata.create_all(bind=engine)


def sync_main_project_schema() -> None:
    """Backfill missing columns on existing local databases."""
    inspector = inspect(engine)
    try:
        columns = {column["name"] for column in inspector.get_columns("main_projects")}
    except Exception:
        return

    if "project_type" in columns:
        return

    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE main_projects ADD COLUMN project_type TEXT NOT NULL DEFAULT 'Full'")
        )


sync_main_project_schema()


def sync_leave_schema() -> None:
    """Backfill missing leave columns on existing local databases."""
    inspector = inspect(engine)
    try:
        columns = {column["name"] for column in inspector.get_columns("leaves")}
    except Exception:
        return

    if "razorpay_applied" in columns:
        return

    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE leaves ADD COLUMN razorpay_applied BOOLEAN NOT NULL DEFAULT FALSE")
        )


sync_leave_schema()


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


def sync_payroll_schema() -> None:
    """Backfill the unpaid_days snapshot column on existing payroll_leave_adjustments tables."""
    inspector = inspect(engine)
    try:
        tables = set(inspector.get_table_names())
    except Exception:
        return
    if "payroll_leave_adjustments" not in tables:
        return  # create_all() will build it with the column already present
    try:
        columns = {column["name"] for column in inspector.get_columns("payroll_leave_adjustments")}
    except Exception:
        return
    if "unpaid_days" in columns:
        return
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE payroll_leave_adjustments ADD COLUMN unpaid_days INTEGER")
        )


sync_payroll_schema()
seed_skills()

app = FastAPI(title="Autonex Resource Planning Tool V2")

if os.environ.get("VERCEL"):
    uploads_dir = Path("/tmp/uploads")
else:
    uploads_dir = Path(__file__).resolve().parents[1] / "uploads"
uploads_dir.mkdir(parents=True, exist_ok=True)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Allow all origins
    allow_credentials=False,
    allow_methods=["*"],   # Allow all HTTP methods
    allow_headers=["*"],   # Allow all headers
)

app.include_router(project_router)
app.include_router(allocation_router)
app.include_router(leave_router)
app.include_router(employee_router)
app.include_router(skills_router)
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
app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")
