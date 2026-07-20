from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
from dotenv import load_dotenv

load_dotenv()

# Get DATABASE_URL from environment, or use SQLite as default
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL is None:
    # Default to SQLite if no DATABASE_URL is provided
    DATABASE_URL = "sqlite:///./autonex.db"
    print(f"Warning: No DATABASE_URL found in .env file. Using SQLite: {DATABASE_URL}")
else:
    # Print confirmation but hide password for security
    db_info = DATABASE_URL.split('@')[0] if '@' in DATABASE_URL else DATABASE_URL
    print(f"Using database: {db_info}...")

# Add connect_args for SQLite or SSL for PostgreSQL
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
elif "neon" in DATABASE_URL:
    # Neon doesn't accept startup options like statement_timeout; omit them
    connect_args = {"connect_timeout": 10}
elif "postgresql" in DATABASE_URL:
    # For regular Postgres (non-Neon) we can set statement_timeout via startup options
    connect_args = {"connect_timeout": 10, "options": "-c statement_timeout=30000"}


def _build_engine():
    """Build the SQLAlchemy engine for the current environment.

    Local development is intentionally NOT configured here. Drop a gitignored
    ``database_local.py`` next to this file (copy ``database_local.py.example``)
    to override the engine on your machine. This committed module only configures
    the deployed environments: Vercel (serverless) and Railway (long-running).
    """
    # Local dev override (gitignored) — used only if the file exists locally.
    try:
        from app.db.database_local import build_local_engine
    except ImportError:
        build_local_engine = None

    if build_local_engine is not None:
        return build_local_engine(DATABASE_URL, connect_args)

    # ── Vercel (serverless) ─────────────────────────────────────────────
    # With Fluid Compute ON, invocations share one warm instance and its
    # module-level globals, so a small pool persists across requests and gets
    # reused instead of opening a new connection every request. Keep it small and
    # recycle often (an instance can be suspended with connections still open, and
    # SQLAlchemy has no pre-suspend close hook like JS's attachDatabasePool).
    # Requires Fluid Compute enabled; without it set DB_FORCE_NULLPOOL=true to fall
    # back to a connection-per-request model.
    if os.getenv("VERCEL") and os.getenv("DB_FORCE_NULLPOOL", "false").lower() == "true":
        from sqlalchemy.pool import NullPool
        return create_engine(
            DATABASE_URL,
            poolclass=NullPool,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
    if os.getenv("VERCEL"):
        return create_engine(
            DATABASE_URL,
            pool_pre_ping=True,
            pool_size=1,        # keep one warm connection (min pool size 1, never max 1)
            max_overflow=4,     # allow short bursts of concurrency within an instance
            pool_recycle=60,    # drop connections after 60s to avoid stale/leaked idle conns
            pool_timeout=10,
            connect_args=connect_args,
        )

    # ── Railway (long-running server) ───────────────────────────────────
    return create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=3,
        max_overflow=5,
        pool_recycle=300,
        pool_timeout=15,
        connect_args=connect_args,
    )


engine = _build_engine()

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
