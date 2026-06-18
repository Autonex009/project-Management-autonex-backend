"""
Manual end-to-end email flow test
==================================
Use this script to verify the full hiring sync pipeline against REAL services:
  - Neon DB (from .env DATABASE_URL)
  - Brevo email API (from .env BREVO_API_KEY / MAIL_FROM)
  - Local frontend at http://localhost:3000

What this does:
  1. Loads .env so all credentials are picked up automatically
  2. Injects one hardcoded test candidate (kisanjena40@gmail.com)
  3. Calls run_sync() — the exact same function the scheduler uses every 12 hours
  4. Creates Employee + User rows in Neon DB
  5. Sends a real welcome email via Brevo with the temp password
  6. Prints the temp password in the terminal so you can log in even if email is slow

Unlike test_hiring_sync.py (which mocks everything), this script makes REAL calls
to Neon DB and Brevo — use it to verify a new environment is wired up correctly.

Run:
    venv/Scripts/python tests/test_email_flow.py

Clean up test account afterwards:
    venv/Scripts/python tests/test_email_flow.py --cleanup
"""

import os
import sys

# ── Step 1: Load .env before any app imports ─────────────────────────────────
# IMPORTANT: load_dotenv() must run before importing app modules because
# database.py reads DATABASE_URL at import time. If .env is loaded after,
# it would connect to the wrong (or missing) database.
from dotenv import load_dotenv
load_dotenv()

# Override PORTAL_URL for local testing — the "Sign In Now" button in the
# welcome email will point to localhost instead of production
os.environ["PORTAL_URL"] = "http://localhost:3000"

# ─────────────────────────────────────────────────────────────────────────────

from unittest.mock import patch

# Import models so all DB tables exist when SessionLocal is used
from app.models.employee import Employee
from app.models.user import User

# Import the same service function the scheduler calls — this is NOT a mock
from app.services.hiring_sync_service import run_sync, _gen_temp_password

# Use the real SessionLocal which connects to Neon DB via .env DATABASE_URL
from app.db.database import SessionLocal


# ── Test candidate ────────────────────────────────────────────────────────────
# Hardcoded data that mimics what the hiring portal would send for a real hire.
# Only fetch_hired_candidates() is mocked — everything else (DB writes, email) is real.

TEST_EMAIL = "kisanjena40@gmail.com"
TEST_NAME  = "Kisan Jena"

TEST_CANDIDATE = {
    "name":           TEST_NAME,
    "email":          TEST_EMAIL,
    "job_title":      "Developer",        # maps to designation="Developer"
    "job_type":       "full-time",        # maps to employee_type="Full-time"
    "hr_status":      "hired",            # only "hired" gets imported
    "application_id": "TEST-001",
    "doc_status":     "complete",
    "phone":          "9999999999",
}


# ── Cleanup ───────────────────────────────────────────────────────────────────

def cleanup():
    """
    Remove the test Employee and User rows from Neon DB.
    Must delete User first because users.employee_id has a FK reference to employees.id —
    deleting Employee first would raise a ForeignKeyViolation.
    """
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == TEST_EMAIL).first()
        emp  = db.query(Employee).filter(Employee.email == TEST_EMAIL).first()

        # Delete User first — it holds the FK reference to Employee
        if user:
            db.delete(user)
            db.flush()                              # execute DELETE immediately so FK is released
            print(f"Deleted User row     → {TEST_EMAIL}")

        if emp:
            db.delete(emp)
            db.flush()
            print(f"Deleted Employee row → {TEST_EMAIL}")

        if user or emp:
            db.commit()
            print("Cleanup done.")
        else:
            print("Nothing to clean up — test account not found.")
    finally:
        db.close()


# ── Main test run ─────────────────────────────────────────────────────────────

def run():
    """
    Run the hiring sync with a hardcoded test candidate.

    Flow:
      fetch_hired_candidates()          <- MOCKED  (returns TEST_CANDIDATE)
      run_sync(db)                      <- REAL    (creates Employee + User in Neon DB)
      try_send_signup_approved_email()  <- REAL    (calls Brevo API)
    """
    db = SessionLocal()
    try:
        # Guard: if this email already exists in DB, skip to avoid duplicate error
        emp_exists  = db.query(Employee).filter(Employee.email == TEST_EMAIL).first()
        user_exists = db.query(User).filter(User.email == TEST_EMAIL).first()

        if emp_exists or user_exists:
            print(f"\n⚠️  {TEST_EMAIL} already exists in the DB.")
            print("Run with --cleanup first, then re-run.\n")
            return

        # Intercept _gen_temp_password so we can print the password in the terminal.
        # The same password is stored (hashed) in the DB and sent in the email.
        captured = {}
        original_gen = __import__(
            "app.services.hiring_sync_service", fromlist=["_gen_temp_password"]
        )._gen_temp_password

        def capturing_gen(length=10):
            # Generate the password using the real function, then capture it
            pwd = original_gen(length)
            captured["password"] = pwd
            return pwd

        print(f"\nSending test email to: {TEST_EMAIL}")
        print(f"Portal URL in email:   {os.environ['PORTAL_URL']}")
        print(f"Sender:                {os.getenv('MAIL_FROM', '(MAIL_FROM not set)')}")
        print("-" * 50)

        # Run the actual sync — only the HTTP call to the hiring portal is mocked
        with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=[TEST_CANDIDATE]), \
             patch("app.services.hiring_sync_service._gen_temp_password", side_effect=capturing_gen):
            result = run_sync(db)

        # Print summary
        print(f"\n{'='*50}")
        print(f"  imported : {result['imported']}")
        print(f"  skipped  : {result['skipped']}")
        print(f"  errors   : {result['errors']}")
        print(f"{'='*50}")

        if result["imported"] == 1:
            detail     = result["details"]["imported"][0]
            email_sent = detail["email_sent"]
            password   = captured.get("password", "(not captured)")

            print(f"\n✅ Account created in Neon DB")
            print(f"   Email       : {TEST_EMAIL}")
            print(f"   Password    : {password}")   # same value that was emailed
            print(f"   Role        : {detail['designation']}")
            print(f"   email_sent  : {email_sent}")

            if email_sent:
                print(f"\n📧 Email sent via Brevo! Check {TEST_EMAIL}")
            else:
                # Account exists — candidate can still log in with the printed password
                print(f"\n⚠️  Email NOT sent (Brevo error — check logs above)")
                print(f"   You can still log in manually with password: {password}")

            print(f"\n▶  Next steps:")
            print(f"   1. Start backend:  uvicorn app.main:app --reload")
            print(f"   2. Start frontend: npm run dev  (localhost:3000)")
            print(f"   3. Log in at http://localhost:3000 with:")
            print(f"        Email    : {TEST_EMAIL}")
            print(f"        Password : {password}")
            print(f"\n   To remove this test account afterwards:")
            print(f"        venv/Scripts/python tests/test_email_flow.py --cleanup")

        elif result["skipped"]:
            # Candidate was skipped — already exists in DB
            print(f"\n⏭  Skipped: {result['details']['skipped'][0]['reason']}")

        elif result["errors"]:
            # Something went wrong during import
            print(f"\n❌ Error: {result['details']['errors'][0]}")

    finally:
        db.close()


if __name__ == "__main__":
    if "--cleanup" in sys.argv:
        cleanup()
    else:
        run()
