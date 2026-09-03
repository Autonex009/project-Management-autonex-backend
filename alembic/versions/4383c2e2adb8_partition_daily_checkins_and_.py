"""partition_daily_checkins_and_materialized_view

Revision ID: 4383c2e2adb8
Revises: ce26ed0bd222
Create Date: 2026-09-03 17:25:20.872938

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4383c2e2adb8'
down_revision: Union[str, Sequence[str], None] = 'ce26ed0bd222'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Rename existing table and its constraints/indexes
    op.rename_table('daily_checkins', 'daily_checkins_old')
    op.execute("ALTER INDEX ix_daily_checkins_checkin_date RENAME TO ix_daily_checkins_old_checkin_date")
    op.execute("ALTER INDEX ix_daily_checkins_employee_id RENAME TO ix_daily_checkins_old_employee_id")
    op.execute("ALTER INDEX ix_daily_checkins_id RENAME TO ix_daily_checkins_old_id")
    try:
        op.execute("ALTER INDEX uq_daily_checkin_employee_date RENAME TO uq_daily_checkins_old_employee_date")
    except Exception:
        pass
    try:
        op.execute("ALTER INDEX idx_daily_checkins_projects RENAME TO idx_daily_checkins_old_projects")
    except Exception:
        pass
    
    
    # 2. Create the partitioned table
    op.execute("""
        CREATE TABLE daily_checkins (
            id SERIAL,
            employee_id INTEGER NOT NULL,
            checkin_date DATE NOT NULL,
            work_mode TEXT NOT NULL,
            project_ids JSONB DEFAULT '[]'::jsonb,
            mood TEXT,
            checked_in_at TIMESTAMP WITH TIME ZONE,
            checked_out_at TIMESTAMP WITH TIME ZONE,
            pm_confirmed_at TIMESTAMP WITH TIME ZONE,
            pm_confirmed_by INTEGER,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
            PRIMARY KEY (id, checkin_date)
        ) PARTITION BY RANGE (checkin_date);
    """)
    
    # 3. Create initial partitions
    op.execute("""
        CREATE TABLE daily_checkins_2026_08 PARTITION OF daily_checkins FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
        CREATE TABLE daily_checkins_2026_09 PARTITION OF daily_checkins FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
        CREATE TABLE daily_checkins_2026_10 PARTITION OF daily_checkins FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
        CREATE TABLE daily_checkins_2026_11 PARTITION OF daily_checkins FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
    """)

    # 4. Copy data over
    op.execute("""
        INSERT INTO daily_checkins (id, employee_id, checkin_date, work_mode, project_ids, mood, checked_in_at, checked_out_at, pm_confirmed_at, pm_confirmed_by, created_at, updated_at)
        SELECT id, employee_id, checkin_date, work_mode, project_ids, mood, checked_in_at, checked_out_at, pm_confirmed_at, pm_confirmed_by, created_at, updated_at
        FROM daily_checkins_old
        WHERE checkin_date >= '2026-08-01';
    """)

    # 5. Fix sequence
    op.execute("SELECT setval('daily_checkins_id_seq', COALESCE((SELECT MAX(id) FROM daily_checkins), 1))")

    # 6. Recreate indexes and constraints
    op.create_index('ix_daily_checkins_id', 'daily_checkins', ['id'])
    op.create_index('ix_daily_checkins_employee_id', 'daily_checkins', ['employee_id'])
    op.create_index('ix_daily_checkins_checkin_date', 'daily_checkins', ['checkin_date'])
    op.create_index('idx_daily_checkins_projects', 'daily_checkins', ['project_ids'], postgresql_using='gin')
    op.create_unique_constraint('uq_daily_checkin_employee_date', 'daily_checkins', ['employee_id', 'checkin_date'])
    
    # 7. Drop old table
    op.drop_table('daily_checkins_old')

    # 8. Create Materialized View
    op.execute("""
        CREATE MATERIALIZED VIEW historical_checkins_matrix AS
        WITH parsed AS (
            SELECT 
                employee_id,
                TO_CHAR(checkin_date, 'YYYY-MM') AS month_year,
                EXTRACT(DAY FROM checkin_date)::TEXT AS day_str,
                jsonb_build_object(
                    'time', TO_CHAR(checked_in_at AT TIME ZONE 'Asia/Kolkata', 'HH24:MI'),
                    'mode', work_mode
                ) AS cell_data
            FROM daily_checkins
        )
        SELECT 
            employee_id,
            month_year,
            jsonb_object_agg(day_str, cell_data) AS checkin_matrix
        FROM parsed
        GROUP BY employee_id, month_year;
    """)

    # 9. Create unique index
    op.execute("""
        CREATE UNIQUE INDEX idx_historical_checkins_matrix_unique 
        ON historical_checkins_matrix(employee_id, month_year);
    """)

def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS historical_checkins_matrix")
    
    op.rename_table('daily_checkins', 'daily_checkins_partitioned')
    op.execute("""
        CREATE TABLE daily_checkins (
            id SERIAL PRIMARY KEY,
            employee_id INTEGER NOT NULL,
            checkin_date DATE NOT NULL,
            work_mode TEXT NOT NULL,
            project_ids JSONB DEFAULT '[]'::jsonb,
            mood TEXT,
            checked_in_at TIMESTAMP WITH TIME ZONE,
            checked_out_at TIMESTAMP WITH TIME ZONE,
            pm_confirmed_at TIMESTAMP WITH TIME ZONE,
            pm_confirmed_by INTEGER,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()
        );
    """)
    op.execute("""
        INSERT INTO daily_checkins 
        SELECT * FROM daily_checkins_partitioned;
    """)
    op.execute("SELECT setval('daily_checkins_id_seq', COALESCE((SELECT MAX(id) FROM daily_checkins), 1))")
    op.drop_table('daily_checkins_partitioned')
    op.create_index('ix_daily_checkins_id', 'daily_checkins', ['id'])
    op.create_index('ix_daily_checkins_employee_id', 'daily_checkins', ['employee_id'])
    op.create_index('ix_daily_checkins_checkin_date', 'daily_checkins', ['checkin_date'])
    op.create_index('idx_daily_checkins_projects', 'daily_checkins', ['project_ids'], postgresql_using='gin')
    op.create_unique_constraint('uq_daily_checkin_employee_date', 'daily_checkins', ['employee_id', 'checkin_date'])
