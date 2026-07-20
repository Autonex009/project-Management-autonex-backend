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

# On Vercel the strategy depends on Fluid Compute:
#   * With Fluid Compute ON, multiple invocations share one warm instance and its
#     module-level globals, so a small connection pool persists across requests and
#     gets reused instead of opening a brand-new connection every request. This is
#     what lets serverless perform like a warm long-running server.
#   * Keep the pool small and recycle often: an instance can be suspended with
#     connections still open (SQLAlchemy has no pre-suspend close hook like JS's
#     attachDatabasePool), so a short pool_recycle limits stale/leaked connections.
# Requires Fluid Compute to be enabled on the Vercel project. Without it, classic
# serverless tears instances down between requests and the pool can't be reused --
# set DB_FORCE_NULLPOOL=true to fall back to a connection-per-request model.
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
elif os.getenv("VERCEL") and os.getenv("DB_FORCE_NULLPOOL", "false").lower() == "true":
    # Fallback: classic serverless with no connection reuse (one conn per request).
    from sqlalchemy.pool import NullPool
    engine = create_engine(
        DATABASE_URL,
        poolclass=NullPool,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
elif os.getenv("VERCEL"):
    # Fluid Compute: reuse a small warm pool across invocations in the same instance.
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=1,        # keep one warm connection (min pool size 1, never max 1)
        max_overflow=4,     # allow short bursts of concurrency within an instance
        pool_recycle=60,    # drop connections after 60s to avoid stale/leaked idle conns
        pool_timeout=10,
        connect_args=connect_args,
    )
else:
    # Long-running server (local / Railway) — a small pool is fine.
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=3,
        max_overflow=5,
        pool_recycle=300,
        pool_timeout=15,
        connect_args=connect_args,
    )

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
