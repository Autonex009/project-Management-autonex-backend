"""sync razorpay_email between users and employees

Revision ID: d1e2f3a4b5c6
Revises: c8d9e0f1a2b3
Create Date: 2026-10-28 18:35:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c8d9e0f1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Ensure columns exist on both tables
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS razorpay_email TEXT;")
    op.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS razorpay_email TEXT;")

    # 2. Reconcile existing data between users and employees
    # Sync from users -> employees where employee is missing it
    op.execute("""
        UPDATE employees e
        SET razorpay_email = u.razorpay_email
        FROM users u
        WHERE u.employee_id = e.id
          AND e.razorpay_email IS NULL
          AND u.razorpay_email IS NOT NULL;
    """)

    # Sync from employees -> users where user is missing it
    op.execute("""
        UPDATE users u
        SET razorpay_email = e.razorpay_email
        FROM employees e
        WHERE u.employee_id = e.id
          AND u.razorpay_email IS NULL
          AND e.razorpay_email IS NOT NULL;
    """)

    # 3. Create bidirectional sync triggers to prevent future drift
    op.execute("""
        CREATE OR REPLACE FUNCTION sync_employee_razorpay_email()
        RETURNS TRIGGER AS $$
        BEGIN
            IF (TG_OP = 'UPDATE' AND NEW.razorpay_email IS DISTINCT FROM OLD.razorpay_email)
               OR (TG_OP = 'INSERT' AND NEW.razorpay_email IS NOT NULL) THEN
                UPDATE users
                SET razorpay_email = NEW.razorpay_email
                WHERE employee_id = NEW.id
                  AND razorpay_email IS DISTINCT FROM NEW.razorpay_email;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("DROP TRIGGER IF EXISTS trg_sync_employee_razorpay_email ON employees;")
    op.execute("""
        CREATE TRIGGER trg_sync_employee_razorpay_email
        AFTER INSERT OR UPDATE OF razorpay_email ON employees
        FOR EACH ROW
        EXECUTE FUNCTION sync_employee_razorpay_email();
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION sync_user_razorpay_email()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.employee_id IS NOT NULL THEN
                IF (TG_OP = 'UPDATE' AND NEW.razorpay_email IS DISTINCT FROM OLD.razorpay_email)
                   OR (TG_OP = 'INSERT' AND NEW.razorpay_email IS NOT NULL) THEN
                    UPDATE employees
                    SET razorpay_email = NEW.razorpay_email
                    WHERE id = NEW.employee_id
                      AND razorpay_email IS DISTINCT FROM NEW.razorpay_email;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("DROP TRIGGER IF EXISTS trg_sync_user_razorpay_email ON users;")
    op.execute("""
        CREATE TRIGGER trg_sync_user_razorpay_email
        AFTER INSERT OR UPDATE OF razorpay_email ON users
        FOR EACH ROW
        EXECUTE FUNCTION sync_user_razorpay_email();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_sync_employee_razorpay_email ON employees;")
    op.execute("DROP FUNCTION IF EXISTS sync_employee_razorpay_email();")
    op.execute("DROP TRIGGER IF EXISTS trg_sync_user_razorpay_email ON users;")
    op.execute("DROP FUNCTION IF EXISTS sync_user_razorpay_email();")
