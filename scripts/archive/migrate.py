"""Quick migration to add new columns to existing leaves table."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine, text

# Read DATABASE_URL from .env
from dotenv import load_dotenv
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
print(f"Connecting to: {DATABASE_URL[:30]}...")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args={"connect_timeout": 10})

def migrate():
    with engine.connect() as conn:
        # Add new columns to leaves table
        for stmt in [
            "ALTER TABLE leaves ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'pending'",
            "ALTER TABLE leaves ADD COLUMN IF NOT EXISTS approved_by INTEGER",
            "ALTER TABLE leaves ADD COLUMN IF NOT EXISTS reason TEXT",
            "ALTER TABLE leaves ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()",
            "ALTER TABLE leaves ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()",
        ]:
            try:
                conn.execute(text(stmt))
                print(f"  OK: {stmt[:60]}")
            except Exception as e:
                print(f"  Skip: {e}")
        conn.commit()
        print("Leaves migration done!")

if __name__ == "__main__":
    migrate()
