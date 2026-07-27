"""
Email service — sends transactional emails via Brevo (formerly Sendinblue).
Uses their HTTP API, so no extra packages are needed (just urllib).
Free tier: 300 emails/day.

Required env vars:
  BREVO_API_KEY   — API key from Brevo dashboard (Settings → API Keys)
  MAIL_FROM       — Verified sender email address
  MAIL_FROM_NAME  — Display name (default: "Autonex AI")
"""
import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


def _send(*, to_email: str, to_name: str, subject: str, html_body: str) -> None:
    """Send a single HTML email via Brevo API. Raises RuntimeError on failure."""
    api_key   = os.getenv("BREVO_API_KEY", "")
    from_addr = os.getenv("MAIL_FROM", "")
    from_name = os.getenv("MAIL_FROM_NAME", "Autonex AI")

    if not api_key:
        raise RuntimeError("BREVO_API_KEY is not configured")
    if not from_addr:
        raise RuntimeError("MAIL_FROM is not configured")

    payload = json.dumps({
        "sender":      {"name": from_name, "email": from_addr},
        "to":          [{"email": to_email, "name": to_name}],
        "subject":     subject,
        "htmlContent": html_body,
        # Disable click tracking so Brevo doesn't wrap reset links in
        # its sendibt2.com redirect, which truncates the token parameter.
        "trackClicks": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        BREVO_API_URL,
        data=payload,
        headers={
            "api-key":      api_key,
            "Content-Type": "application/json",
            "Accept":       "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            logger.info("[email] Brevo accepted message to %s: %s", to_email, body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        logger.error("[email] Brevo API error %s: %s", exc.code, detail)
        raise RuntimeError(f"Brevo API error {exc.code}: {detail}") from exc
    except OSError as exc:
        raise RuntimeError(f"Could not reach Brevo API: {exc}") from exc


# ── Password reset ────────────────────────────────────────────────────────────

def send_password_reset_email(*, to_email: str, to_name: str, reset_link: str) -> None:
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{ font-family: Arial, sans-serif; background: #f4f4f7; margin: 0; padding: 0; }}
    .container {{ max-width: 560px; margin: 40px auto; background: #fff; border-radius: 8px;
                  padding: 40px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
    h2 {{ color: #1a1a2e; margin-top: 0; }}
    .btn {{ display: inline-block; margin-top: 24px; background: #4f46e5; color: #fff !important;
            text-decoration: none; padding: 12px 28px; border-radius: 6px; font-size: 15px; }}
    p {{ color: #374151; line-height: 1.6; }}
    .note {{ font-size: 13px; color: #6b7280; margin-top: 20px; }}
    .footer {{ margin-top: 32px; font-size: 12px; color: #9ca3af;
               border-top: 1px solid #e5e7eb; padding-top: 16px; }}
  </style>
</head>
<body>
  <div class="container">
    <h2>Reset your Autonex AI password</h2>
    <p>Hi {to_name.split()[0]},</p>
    <p>We received a request to reset the password for your account.
       Click the button below to choose a new password.</p>
    <a href="{reset_link}" class="btn">Reset Password</a>
    <p class="note">
      This link expires in <strong>15 minutes</strong>.
      If you did not request a password reset, you can safely ignore this email.
    </p>
    <div class="footer">
      <p>Autonex AI &mdash; {os.getenv("MAIL_FROM", "")}</p>
    </div>
  </div>
</body>
</html>"""
    _send(
        to_email=to_email,
        to_name=to_name,
        subject="Reset your Autonex AI password",
        html_body=html,
    )


def send_signup_approved_email(*, to_email: str, to_name: str, temp_password: str, portal_url: str) -> None:
    first = to_name.split()[0]
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{ font-family: Arial, sans-serif; background: #f4f4f7; margin: 0; padding: 0; }}
    .container {{ max-width: 600px; margin: 40px auto; background: #fff; border-radius: 10px;
                  overflow: hidden; box-shadow: 0 2px 12px rgba(0,0,0,0.1); }}
    .header {{ background: linear-gradient(135deg, #1a3fa8, #2b67ff); padding: 36px 40px; text-align: center; }}
    .header h1 {{ color: #fff; margin: 0; font-size: 22px; }}
    .header p {{ color: rgba(255,255,255,0.8); margin: 8px 0 0; font-size: 14px; }}
    .body {{ padding: 36px 40px; }}
    h2 {{ color: #1a1a2e; font-size: 17px; margin-top: 0; }}
    p {{ color: #374151; line-height: 1.7; font-size: 15px; }}
    .creds {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 20px 24px; margin: 20px 0; }}
    .creds table {{ width: 100%; border-collapse: collapse; }}
    .creds td {{ padding: 8px 0; font-size: 14px; color: #374151; vertical-align: top; }}
    .creds td:first-child {{ font-weight: 600; color: #1e293b; width: 160px; }}
    .creds a {{ color: #2b67ff; text-decoration: none; }}
    .creds code {{ background: #e0e7ff; color: #3730a3; padding: 3px 8px; border-radius: 4px; font-family: monospace; font-size: 14px; }}
    .btn {{ display: inline-block; margin-top: 4px; background: #4f46e5; color: #fff !important;
            text-decoration: none; padding: 12px 28px; border-radius: 6px; font-size: 15px; font-weight: 600; }}
    .warning {{ background: #fffbeb; border: 1px solid #fcd34d; border-radius: 8px;
                padding: 14px 18px; margin: 20px 0; font-size: 14px; color: #92400e; }}
    .footer {{ background: #f8fafc; border-top: 1px solid #e5e7eb; padding: 20px 40px;
               font-size: 12px; color: #9ca3af; text-align: center; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>🎉 Welcome to Autonex AI!</h1>
      <p>Your account has been approved</p>
    </div>
    <div class="body">
      <p>Hi {first},</p>
      <p>Great news! Your signup request has been <strong>approved</strong>.
         Your employee account is now active. Use the credentials below to sign in.</p>
      <div class="creds">
        <table>
          <tr><td>Portal</td><td><a href="{portal_url}">{portal_url}</a></td></tr>
          <tr><td>Email</td><td>{to_email}</td></tr>
          <tr><td>Temp Password</td><td><code>{temp_password}</code></td></tr>
        </table>
      </div>
      <div class="warning">
        ⚠️ <strong>Action Required:</strong> Please reset your password immediately after first login.
      </div>
      <a href="{portal_url}" class="btn">Sign In Now</a>
      <p style="font-size:13px;color:#6b7280;margin-top:14px;">
        Reset password: <a href="https://pmportal.autonexai360.com/forgot-password" style="color:#2b67ff;">
        pmportal.autonexai360.com/forgot-password</a>
      </p>
      <p>If you have any questions, reach out in <strong>#autonex-tool-support</strong> on Slack.</p>
      <p>Best regards,<br><strong>The Autonex AI Team</strong></p>
    </div>
    <div class="footer"><p>Autonex AI &mdash; {os.getenv("MAIL_FROM", "")}</p></div>
  </div>
</body>
</html>"""
    _send(to_email=to_email, to_name=to_name, subject="Your Autonex AI account is approved! 🎉", html_body=html)


def send_signup_rejected_email(*, to_email: str, to_name: str, reason: str = "") -> None:
    first = to_name.split()[0]
    reason_block = f"<p><strong>Reason:</strong> {reason}</p>" if reason else ""
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{ font-family: Arial, sans-serif; background: #f4f4f7; margin: 0; padding: 0; }}
    .container {{ max-width: 560px; margin: 40px auto; background: #fff; border-radius: 8px;
                  padding: 40px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
    h2 {{ color: #1a1a2e; margin-top: 0; }}
    p {{ color: #374151; line-height: 1.6; }}
    .footer {{ margin-top: 32px; font-size: 12px; color: #9ca3af;
               border-top: 1px solid #e5e7eb; padding-top: 16px; }}
  </style>
</head>
<body>
  <div class="container">
    <h2>Signup Request Update</h2>
    <p>Hi {first},</p>
    <p>We reviewed your signup request for Autonex AI and unfortunately we are unable to
       approve it at this time.</p>
    {reason_block}
    <p>If you believe this is a mistake or have questions, please contact your manager
       or reach out via <strong>#autonex-tool-support</strong>.</p>
    <p>Best regards,<br><strong>The Autonex AI Team</strong></p>
    <div class="footer"><p>Autonex AI &mdash; {os.getenv("MAIL_FROM", "")}</p></div>
  </div>
</body>
</html>"""
    _send(to_email=to_email, to_name=to_name, subject="Update on your Autonex AI signup request", html_body=html)


def try_send_signup_approved_email(**kwargs) -> bool:
    try:
        send_signup_approved_email(**kwargs)
        return True
    except Exception as exc:
        logger.warning("[email] Signup approved email failed: %s", exc)
        return False


def try_send_signup_rejected_email(**kwargs) -> bool:
    try:
        send_signup_rejected_email(**kwargs)
        return True
    except Exception as exc:
        logger.warning("[email] Signup rejected email failed: %s", exc)
        return False


def try_send_password_reset_email(*, to_email: str, to_name: str, reset_link: str) -> bool:
    """Returns True on success, False on failure (logs the error)."""
    try:
        send_password_reset_email(to_email=to_email, to_name=to_name, reset_link=reset_link)
        return True
    except Exception as exc:
        logger.warning("[email] Password reset email failed for %s: %s", to_email, exc)
        return False


# ── Referral emails ───────────────────────────────────────────────────────────

def send_referral_confirmation_email(
    *, referrer_name: str, referrer_email: str, candidate_name: str, position: str
) -> None:
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{ font-family: Arial, sans-serif; background: #f4f4f7; margin: 0; padding: 0; }}
    .container {{ max-width: 560px; margin: 40px auto; background: #fff; border-radius: 8px;
                  padding: 40px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
    h2 {{ color: #1a1a2e; margin-top: 0; }}
    .badge {{ display: inline-block; background: #d1fae5; color: #065f46;
              border-radius: 20px; padding: 4px 14px; font-size: 13px; font-weight: 600; margin-bottom: 20px; }}
    p {{ color: #374151; line-height: 1.6; }}
    .detail {{ background: #f9fafb; border-left: 4px solid #10b981; border-radius: 4px;
               padding: 14px 18px; margin: 20px 0; }}
    .detail strong {{ color: #065f46; }}
    .footer {{ font-size: 12px; color: #9ca3af; margin-top: 24px; border-top: 1px solid #e5e7eb; padding-top: 16px; }}
  </style>
</head>
<body>
  <div class="container">
    <span class="badge">Referral Submitted</span>
    <h2>Thanks for your referral, {referrer_name}!</h2>
    <p>We've received your referral and our hiring team will review it shortly.</p>
    <div class="detail">
      <p><strong>Candidate:</strong> {candidate_name}</p>
      <p><strong>Position:</strong> {position}</p>
    </div>
    <p>We'll keep you updated as the application progresses. You can track the status in your
       <strong>Autonex Employee Portal → Referrals</strong> page.</p>
    <p>Best regards,<br><strong>The Autonex Hiring Team</strong></p>
    <div class="footer"><p>Autonex AI &mdash; {os.getenv("MAIL_FROM", "")}</p></div>
  </div>
</body>
</html>"""
    _send(
        to_email=referrer_email,
        to_name=referrer_name,
        subject=f"Referral received: {candidate_name} for {position}",
        html_body=html,
    )


def send_referral_status_update_email(
    *, referrer_name: str, referrer_email: str, candidate_name: str, position: str, new_status: str
) -> None:
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{ font-family: Arial, sans-serif; background: #f4f4f7; margin: 0; padding: 0; }}
    .container {{ max-width: 560px; margin: 40px auto; background: #fff; border-radius: 8px;
                  padding: 40px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
    h2 {{ color: #1a1a2e; margin-top: 0; }}
    .badge {{ display: inline-block; background: #eff6ff; color: #1d4ed8;
              border-radius: 20px; padding: 4px 14px; font-size: 13px; font-weight: 600; margin-bottom: 20px; }}
    p {{ color: #374151; line-height: 1.6; }}
    .detail {{ background: #f9fafb; border-left: 4px solid #3b82f6; border-radius: 4px;
               padding: 14px 18px; margin: 20px 0; }}
    .status {{ font-size: 15px; font-weight: 700; color: #1d4ed8; }}
    .footer {{ font-size: 12px; color: #9ca3af; margin-top: 24px; border-top: 1px solid #e5e7eb; padding-top: 16px; }}
  </style>
</head>
<body>
  <div class="container">
    <span class="badge">Referral Update</span>
    <h2>An update on your referral</h2>
    <p>Hi {referrer_name}, here's the latest on the candidate you referred:</p>
    <div class="detail">
      <p><strong>Candidate:</strong> {candidate_name}</p>
      <p><strong>Position:</strong> {position}</p>
      <p>New status: <span class="status">{new_status}</span></p>
    </div>
    <p>You can view full details in your <strong>Autonex Employee Portal → Referrals</strong> page.</p>
    <p>Best regards,<br><strong>The Autonex Hiring Team</strong></p>
    <div class="footer"><p>Autonex AI &mdash; {os.getenv("MAIL_FROM", "")}</p></div>
  </div>
</body>
</html>"""
    _send(
        to_email=referrer_email,
        to_name=referrer_name,
        subject=f"Referral update: {candidate_name} — {new_status}",
        html_body=html,
    )


def try_send_referral_confirmation_email(**kwargs) -> bool:
    try:
        send_referral_confirmation_email(**kwargs)
        return True
    except Exception as exc:
        logger.warning("[email] Referral confirmation email failed: %s", exc)
        return False


def try_send_referral_status_update_email(**kwargs) -> bool:
    try:
        send_referral_status_update_email(**kwargs)
        return True
    except Exception as exc:
        logger.warning("[email] Referral status update email failed: %s", exc)
        return False
