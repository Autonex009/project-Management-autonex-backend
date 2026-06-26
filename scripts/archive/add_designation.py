"""
Migration script to add designation column to employees table.
"""
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set")
    exit(1)

engine = create_engine(DATABASE_URL)

print("Adding designation column to employees...")

try:
    with engine.connect() as conn:
        conn.execute(text("""
            ALTER TABLE employees 
            ADD COLUMN IF NOT EXISTS designation TEXT DEFAULT 'Annotator'
        """))
        conn.commit()
        print("✓ Designation column added successfully!")
except Exception as e:
    print(f"❌ Error: {e}")
    exit(1)
