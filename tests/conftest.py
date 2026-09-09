import os
import sys

# Ensure project root is on sys.path so 'app' module can be imported anywhere
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ["JWT_SECRET_KEY"] = "test-secret-key-for-pytest"
os.environ["OTP_SECRET_KEY"] = "test-otp-secret-key-for-pytest"

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

# Teach SQLAlchemy SQLite compiler how to render PostgreSQL JSONB in SQLite test runs
@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"

# Override autoincrement flag on DailyCheckIn.id for SQLite test sessions
from app.models.daily_checkin import DailyCheckIn
DailyCheckIn.__table__.c.id.autoincrement = "auto"