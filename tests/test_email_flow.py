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
from app.models.signup_request import SignupRequest

# Import the same service function the scheduler calls — this is NOT a mock
from app.services.hiring_sync_service import run_sync

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
    deleting the SignupRequest row.
    """
    db = SessionLocal()
    try:
        req = db.query(SignupRequest).filter(SignupRequest.email == TEST_EMAIL).first()
        if req:
            db.delete(req)
            db.commit()
            print(f"Deleted SignupRequest → {TEST_EMAIL}")
        else:
            print("Nothing to clean up — test signup request not found.")
    finally:
        db.close()


# ── Main test run ─────────────────────────────────────────────────────────────

def run():
    """
    Run the hiring sync with a hardcoded test candidate.

    New flow:
      fetch_hired_candidates()  <- MOCKED  (returns TEST_CANDIDATE)
      run_sync(db)              <- REAL    (creates SignupRequest in Neon DB)

    Email is sent when admin approves the signup request — not here.
    Go to Admin panel → Signup Requests → Approve to test the email.
    """
    db = SessionLocal()
    try:
        # Guard: if a signup request already exists, skip
        existing = db.query(SignupRequest).filter(SignupRequest.email == TEST_EMAIL).first()
        if existing:
            print(f"\n⚠️  SignupRequest for {TEST_EMAIL} already exists (status: {existing.status}).")
            print("Run with --cleanup first, then re-run.\n")
            return

        print(f"\nCreating SignupRequest for: {TEST_EMAIL}")
        print("-" * 50)

        # Run the actual sync — only the HTTP call to the hiring portal is mocked
        with patch("app.services.hiring_sync_service.fetch_hired_candidates", return_value=[TEST_CANDIDATE]):
            result = run_sync(db)

        # Print summary
        print(f"\n{'='*50}")
        print(f"  imported : {result['imported']}")
        print(f"  skipped  : {result['skipped']}")
        print(f"  errors   : {result['errors']}")
        print(f"{'='*50}")

        if result["imported"] == 1:
            detail = result["details"]["imported"][0]
            print(f"\n✅ SignupRequest created (status: pending)")
            print(f"   Email         : {TEST_EMAIL}")
            print(f"   Designation   : {detail['designation']}")
            print(f"   Employee type : {detail['employee_type']}")
            print(f"\n▶  Next steps to test email:")
            print(f"   1. Go to Admin panel → Signup Requests")
            print(f"   2. Find {TEST_EMAIL} → click Approve")
            print(f"   3. Welcome email with temp password will be sent via Brevo")
            print(f"\n   To remove this test request afterwards:")
            print(f"        venv/Scripts/python tests/test_email_flow.py --cleanup")

        elif result["skipped"]:
            print(f"\n⏭  Skipped: {result['details']['skipped'][0]['reason']}")

        elif result["errors"]:
            print(f"\n❌ Error: {result['details']['errors'][0]}")

    finally:
        db.close()


if __name__ == "__main__":
    if "--cleanup" in sys.argv:
        cleanup()
    else:
        run()
