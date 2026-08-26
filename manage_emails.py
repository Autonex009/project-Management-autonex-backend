"""
manage_emails.py — Centralized email utility for Autonex AI.

Replaces:
  - send_test_email.py
  - send_welcome_email.py
  - send_welcome_bulk.py

Usage:
  python manage_emails.py test <recipient_email>
  python manage_emails.py welcome <recipient_email> <recipient_name> [role]
  python manage_emails.py bulk-welcome
"""

import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.error
from pathlib import Path

try:
    import psycopg2
except ImportError:
    psycopg2 = None

# ── Load env ──────────────────────────────────────────────────────────────────
for env_file in [".env.production", ".env"]:
    env_path = Path(__file__).parent / env_file
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"'))

FROM_EMAIL = os.getenv("MAIL_FROM", "")
FROM_NAME  = os.getenv("MAIL_FROM_NAME", "Autonex AI")
BREVO_KEY  = os.getenv("BREVO_API_KEY", "")
DB_URL     = os.getenv("DATABASE_URL", "")

if not BREVO_KEY:
    print("ERROR: BREVO_API_KEY not found. Make sure .env or .env.production exists.")
    sys.exit(1)

ROLE_PORTALS   = {
    "admin":    "https://autonex-frontend.vercel.app/login/admin",
    "pm":       "https://autonex-frontend.vercel.app/login/pm",
    "employee": "https://autonex-frontend.vercel.app/login/employee",
}
ROLE_LABELS = {"admin": "Admin", "pm": "Program Manager", "employee": "Employee"}
RESET_URL   = "https://autonex-frontend.vercel.app/forgot-password"


def get_base_html(title, body_content):
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{ font-family: Arial, sans-serif; background: #f4f4f7; margin: 0; padding: 0; }}
    .container {{ max-width: 600px; margin: 40px auto; background: #ffffff;
                  border-radius: 10px; overflow: hidden;
                  box-shadow: 0 2px 12px rgba(0,0,0,0.1); }}
    .header {{ background: linear-gradient(135deg, #1a3fa8, #2b67ff);
               padding: 36px 40px; text-align: center; }}
    .header h1 {{ color: #ffffff; margin: 0; font-size: 24px; letter-spacing: -0.3px; }}
    .header p  {{ color: rgba(255,255,255,0.8); margin: 8px 0 0; font-size: 14px; }}
    .body {{ padding: 36px 40px; }}
    h2 {{ color: #1a1a2e; font-size: 18px; margin-top: 0; }}
    p  {{ color: #374151; line-height: 1.7; font-size: 15px; }}
    ul {{ color: #374151; line-height: 2; font-size: 15px; padding-left: 20px; }}
    .creds {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;
              padding: 20px 24px; margin: 24px 0; }}
    .creds table {{ width: 100%; border-collapse: collapse; }}
    .creds td {{ padding: 8px 0; font-size: 14px; color: #374151; vertical-align: top; }}
    .creds td:first-child {{ font-weight: 600; color: #1e293b; width: 160px; }}
    .creds a {{ color: #2b67ff; text-decoration: none; }}
    .creds code {{ background: #e0e7ff; color: #3730a3; padding: 3px 8px;
                   border-radius: 4px; font-family: monospace; font-size: 14px; }}
    .btn {{ display: inline-block; margin-top: 4px; background: #4f46e5; color: #fff !important;
            text-decoration: none; padding: 12px 28px; border-radius: 6px;
            font-size: 15px; font-weight: 600; }}
    .warning {{ background: #fffbeb; border: 1px solid #fcd34d; border-radius: 8px;
                padding: 14px 18px; margin: 24px 0; font-size: 14px; color: #92400e; }}
    .footer {{ background: #f8fafc; border-top: 1px solid #e5e7eb;
               padding: 20px 40px; font-size: 12px; color: #9ca3af; text-align: center; }}
    .badge {{ display: inline-block; background: #4f46e5; color: #fff; padding: 6px 16px;
              border-radius: 20px; font-size: 13px; margin-top: 8px; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>{title}</h1>
      <p>Resource Planning &amp; Project Allocation Tool</p>
    </div>
    <div class="body">
      {body_content}
    </div>
    <div class="footer">
      <p>Autonex AI &mdash; {FROM_EMAIL}</p>
      <p>This is an automated message. Please do not reply directly to this email.</p>
    </div>
  </div>
</body>
</html>"""


def get_welcome_html(to_name, role):
    portal_url = ROLE_PORTALS.get(role, ROLE_PORTALS["employee"])
    role_label = ROLE_LABELS.get(role, "Employee")
    first_name = to_name.split()[0] if to_name else "there"

    body_content = f"""
      <p>Hi {first_name},</p>
      <p>We are excited to announce the <strong>official launch</strong> of the
         <strong>Autonex Resource Planning and Project Allocation Tool!</strong></p>
      <h2>What is the Autonex Portal?</h2>
      <p>This new platform is designed to streamline our project planning and resource
         allocation for all data annotation workflows. Moving forward, this will be your
         central hub to:</p>
      <ul>
        <li>View your personal project assignments</li>
        <li>Check your weekly tasks and hourly targets</li>
        <li>Track your personal productivity stats</li>
        <li>Submit and track your leave requests</li>
      </ul>
      <h2>Your Account Details</h2>
      <p>You can access the portal using your company email and your assigned role:</p>
      <div class="creds">
        <table>
          <tr>
            <td>Portal Link</td>
            <td><a href="{portal_url}">{portal_url}</a></td>
          </tr>
          <tr>
            <td>Role</td>
            <td>{role_label}</td>
          </tr>
          <tr>
            <td>Username</td>
            <td>Your company email address</td>
          </tr>
        </table>
      </div>
      <div class="warning">
        ⚠️ <strong>Action Required:</strong> To access your account, you must first set a secure password using the link below.
      </div>
      <h2>Set Your Password</h2>
      <p>Click the button below to set up your password and access the dashboard:</p>
      <a href="{RESET_URL}" class="btn">Set My Password</a>
      <p style="font-size:13px; color:#6b7280; margin-top:14px;">
        Or copy this link: <a href="{RESET_URL}" style="color:#2b67ff;">{RESET_URL}</a>
      </p>
      <p>If you run into any issues logging in, encounter bugs, or have questions about
         your assignments, please reach out in our dedicated Slack channel:
         <strong>#autonex-tool-support</strong>.</p>
      <p>Thank you for your cooperation as we roll out this new system!</p>
      <p>Best regards,<br>
         <strong>The Autonex AI Team</strong><br>
         AutonexAI</p>
    """
    return get_base_html("🚀 Welcome to the Autonex Portal", body_content)


def get_test_html(to_email, to_name):
    body_content = f"""
      <h2>Test Email from Autonex AI</h2>
      <p>Hi {to_name},</p>
      <p>This is a test email sent from the Autonex AI platform to verify that the email
         delivery system is working correctly.</p>
      <span class="badge">Email delivery OK</span>
      <p>From: <strong>{FROM_NAME}</strong> &lt;{FROM_EMAIL}&gt;<br>
         To:   <strong>{to_name}</strong> &lt;{to_email}&gt;</p>
    """
    return get_base_html("🔧 Autonex AI — Test Email", body_content)


def send_email(to_email, to_name, subject, html_content):
    payload = json.dumps({
        "sender":      {"name": FROM_NAME, "email": FROM_EMAIL},
        "to":          [{"email": to_email, "name": to_name}],
        "subject":     subject,
        "htmlContent": html_content,
        "trackClicks": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=payload,
        headers={
            "api-key":      BREVO_KEY,
            "Content-Type": "application/json",
            "Accept":       "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode()


def main():
    parser = argparse.ArgumentParser(description="Centralized email utility for Autonex AI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Test Email
    test_parser = subparsers.add_parser("test", help="Send a test email")
    test_parser.add_argument("recipient_email", help="Recipient's email address")

    # Welcome Email (Single)
    welcome_parser = subparsers.add_parser("welcome", help="Send a single welcome email")
    welcome_parser.add_argument("recipient_email", help="Recipient's email address")
    welcome_parser.add_argument("recipient_name", help="Recipient's full name")
    welcome_parser.add_argument("role", nargs="?", default="employee", help="User role (employee, pm, admin)")

    # Welcome Email (Bulk)
    subparsers.add_parser("bulk-welcome", help="Send welcome emails to all active users")

    args = parser.parse_args()

    if args.command == "test":
        print(f"Sending test email to {args.recipient_email} ...")
        html_content = get_test_html(args.recipient_email, "Test Recipient")
        try:
            res = send_email(args.recipient_email, "Test Recipient", "Autonex AI — Test Email", html_content)
            print(f"[Brevo] Accepted: {res}")
            print("Done. Check inbox.")
        except urllib.error.HTTPError as exc:
            print(f"Brevo error {exc.code}: {exc.read().decode()}")
            sys.exit(1)

    elif args.command == "welcome":
        role = args.role.lower()
        print(f"Sending welcome email to {args.recipient_email} (role: {role}) ...")
        html_content = get_welcome_html(args.recipient_name, role)
        subject = "Welcome to the New Autonex Portal! 🚀 (Action Required: Setup Your Account)"
        try:
            res = send_email(args.recipient_email, args.recipient_name, subject, html_content)
            print(f"[Brevo] Accepted: {res}")
            print("Done. Check inbox.")
        except urllib.error.HTTPError as exc:
            print(f"Brevo error {exc.code}: {exc.read().decode()}")
            sys.exit(1)

    elif args.command == "bulk-welcome":
        if not DB_URL:
            print("ERROR: DATABASE_URL not found. Required for bulk emailing.")
            sys.exit(1)
        if not psycopg2:
            print("ERROR: psycopg2 module is not installed. Please install it to use bulk-welcome.")
            sys.exit(1)
            
        print("Fetching users from database...")
        try:
            conn = psycopg2.connect(DB_URL)
            cur  = conn.cursor()
            cur.execute("SELECT name, email, role FROM users WHERE is_active = true ORDER BY role, name")
            users = cur.fetchall()
            cur.close()
            conn.close()
        except Exception as e:
            print(f"Database error: {e}")
            sys.exit(1)
        
        print(f"Sending welcome emails to {len(users)} users...\n")
        
        sent = failed = 0
        subject = "Welcome to the New Autonex Portal! 🚀 (Action Required: Setup Your Account)"
        for name, email, role in users:
            try:
                html_content = get_welcome_html(name, role)
                send_email(email, name, subject, html_content)
                print(f"  ✓  [{role:8s}] {email}")
                sent += 1
                time.sleep(0.15)
            except Exception as exc:
                print(f"  ✗  [{role:8s}] {email}  ERROR: {exc}")
                failed += 1
        
        print(f"\nDone. Sent: {sent}  Failed: {failed}")

if __name__ == "__main__":
    main()
