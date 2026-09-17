"""
Script to update an employee's Slack Member ID.
Usage:
    python scripts/update_employee_slack_id.py
"""
import sys
import os

# Add parent directory to sys.path so app imports work
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.database import SessionLocal
from app.models.user import User
from app.models.employee import Employee
from app.models.audit_log import AuditLog

EMPLOYEE_EMAIL = "nikhilg@autonexai360.com"
NEW_SLACK_USER_ID = "U096Y9Q9ABD"

def main():
    db = SessionLocal()
    try:
        employee = db.query(Employee).filter(Employee.email == EMPLOYEE_EMAIL).first()
        if not employee:
            print(f"Error: Employee with email '{EMPLOYEE_EMAIL}' not found.")
            sys.exit(1)

        old_slack_id = employee.slack_user_id
        print(f"Found employee: {employee.name} (ID: {employee.id}, Email: {employee.email})")
        print(f"Current slack_user_id: {old_slack_id}")
        print(f"New slack_user_id:     {NEW_SLACK_USER_ID}")

        employee.slack_user_id = NEW_SLACK_USER_ID

        # Add audit log entry
        audit_entry = AuditLog(
            actor_id=None,
            actor_name="System Script",
            actor_email="admin@autonexai360.com",
            actor_role="admin",
            action="employee.updated",
            category="Employees",
            action_type="Updated",
            entity_type="employee",
            entity_id=str(employee.id),
            entity_name=employee.name,
            subject_employee_id=employee.id,
            subject_name=employee.name,
            details={
                "Slack user ID": {"from": old_slack_id, "to": NEW_SLACK_USER_ID}
            },
            summary=f"Updated Slack user ID for {employee.name} from {old_slack_id} to {NEW_SLACK_USER_ID}",
        )
        db.add(audit_entry)

        db.commit()
        db.refresh(employee)

        print("\nSuccess! Database updated successfully.")
        print(f"Verified slack_user_id in DB: {employee.slack_user_id}")
    except Exception as e:
        db.rollback()
        print(f"Error occurred: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()

if __name__ == "__main__":
    main()
