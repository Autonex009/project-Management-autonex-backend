import os
import sys
from sqlalchemy import create_engine, MetaData, Table, text

# Set sys.path to backend root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db.database import Base
# Import all models to ensure they are registered in Base.metadata
from app.models import (
    project, allocation, leave, employee, parent_project, user, sub_project,
    guideline, side_project, skill, notification, wfh, signup_request,
    referral, payroll, performance_review, onboarding, company_settings, wifi_network
)

PG_URL = "postgresql://neondb_owner:npg_jNZtRV7SYv5w@ep-sweet-block-and24q7d-pooler.c-6.us-east-1.aws.neon.tech/neondb?sslmode=require"
SQLITE_URL = "sqlite:///autonex.db"

def sync():
    print("Connecting to databases...")
    pg_engine = create_engine(PG_URL)
    sqlite_engine = create_engine(SQLITE_URL)
    
    print("Creating all tables in local SQLite (if not exist)...")
    Base.metadata.create_all(bind=sqlite_engine)
    
    # Reflect tables from postgres
    pg_metadata = MetaData()
    pg_metadata.reflect(bind=pg_engine)
    
    # Empty SQLite tables first to prevent Unique constraint failures
    with sqlite_engine.connect() as sqlite_conn:
        # Disable foreign key checks during clear and sync in SQLite
        sqlite_conn.execute(text("PRAGMA foreign_keys = OFF;"))
        sqlite_conn.commit()

        # Sync tables
        for table_name in Base.metadata.tables.keys():
            print(f"Syncing table: {table_name}...")
            sqlite_table = Base.metadata.tables[table_name]
            
            # Read from postgres
            try:
                pg_table = pg_metadata.tables[table_name]
            except KeyError:
                print(f"  Table {table_name} not found in Postgres. Skipping.")
                continue
                
            # Clear target table in SQLite
            sqlite_conn.execute(sqlite_table.delete())
            sqlite_conn.commit()
            
            # Fetch from Postgres
            with pg_engine.connect() as pg_conn:
                rows = pg_conn.execute(pg_table.select()).fetchall()
                
            if not rows:
                print(f"  No rows to sync in {table_name}.")
                continue
                
            # Insert into sqlite
            columns = [col.name for col in sqlite_table.columns]
            sqlite_rows = []
            for row in rows:
                row_dict = {}
                for col in columns:
                    val = getattr(row, col, None)
                    row_dict[col] = val
                sqlite_rows.append(row_dict)
                
            sqlite_conn.execute(sqlite_table.insert(), sqlite_rows)
            sqlite_conn.commit()
            
            print(f"  Successfully synced {len(sqlite_rows)} rows to table {table_name}.")
            
        # Re-enable foreign key checks
        sqlite_conn.execute(text("PRAGMA foreign_keys = ON;"))
        sqlite_conn.commit()
        
    print("\nSync completed successfully! Local SQLite database is now identical to production database.")

if __name__ == "__main__":
    sync()
