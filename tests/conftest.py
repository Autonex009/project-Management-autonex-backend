import os

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

from app.models.daily_checkin import DailyCheckIn

os.environ["JWT_SECRET_KEY"] = "test-secret-key-for-pytest"
os.environ["OTP_SECRET_KEY"] = "test-otp-secret-key-for-pytest"


# Teach SQLAlchemy SQLite compiler how to render PostgreSQL JSONB in test runs
@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"
# Override autoincrement flag on DailyCheckIn.id for SQLite test sessions
from app.models.daily_checkin import DailyCheckIn
DailyCheckIn.__table__.c.id.autoincrement = "auto"