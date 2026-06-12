"""
Scrub a DEV / STAGING database copy so it carries no real salary data.

Run ONLY against a non-production database (e.g. a Neon `dev` branch). It nulls
the salary columns; everything else is left intact so developers get realistic,
production-shaped data without the sensitive numbers.

Usage:
    python scrub_dev_db.py --url "postgresql://...DEV-BRANCH..." --yes

Safety:
  • Requires the explicit --url and --yes flags.
  • Prints the target host so you can eyeball it before committing.
  • Refuses to run if the target host matches PROD_DB_HOST (set that env var to
    your production host as a guard, e.g. export PROD_DB_HOST=ep-sweet-...neon.tech).

This NEVER touches production unless you point --url at it — so don't.
"""
import argparse
import os
import sys
from urllib.parse import urlparse

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError


def _forbidden_hosts() -> set:
    """Hosts this tool must NEVER scrub — auto-derived so prod is blocked by default.

    Sources: PROD_DB_HOST env (comma-separated), the DATABASE_URL env, and any
    DATABASE_URL found in local .env / .env.production / .env.local. Because the
    project owner's machine has the prod URL in .env.production, the prod host is
    blocked automatically — no flag to remember.
    """
    hosts = set()
    for h in (os.getenv("PROD_DB_HOST") or "").split(","):
        if h.strip():
            hosts.add(h.strip())
    candidates = [os.getenv("DATABASE_URL")]
    for fname in (".env", ".env.production", ".env.local"):
        try:
            with open(fname) as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("DATABASE_URL") and "=" in line:
                        candidates.append(line.split("=", 1)[1].strip().strip('"').strip("'"))
        except FileNotFoundError:
            pass
    for c in candidates:
        if not c:
            continue
        try:
            h = urlparse(c.strip()).hostname
            if h:
                hosts.add(h)
        except Exception:
            pass
    return hosts

# Obvious example/placeholder fragments — if the URL still contains these, the
# user pasted the sample instead of their real connection string.
PLACEHOLDER_TOKENS = ("user:pass", "ep-xxxx", "ep-xxx", "<", ">", "example.com", "dev-branch-url")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrub salary data from a dev/staging DB.")
    parser.add_argument("--url", required=True, help="DEV/staging database URL (must NOT be prod)")
    parser.add_argument("--yes", action="store_true", help="confirm the target is a dev/staging DB")
    args = parser.parse_args()

    url = args.url.strip()
    # SQLAlchemy 2.x rejects the postgres:// scheme Neon sometimes hands out.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    low = url.lower()
    if any(tok in low for tok in PLACEHOLDER_TOKENS):
        sys.exit(
            "That's the EXAMPLE url, not your real one. Copy your actual dev-branch\n"
            "connection string from the Neon console (Branches -> dev -> Connect /\n"
            "Connection Details) and paste it inside the quotes. A real one looks like:\n"
            "  postgresql://neondb_owner:npg_AbC123@ep-cool-name-12345678-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require"
        )

    parsed = urlparse(url)
    host = parsed.hostname
    if not host or not parsed.scheme.startswith("postgresql"):
        sys.exit(
            "Could not parse a Postgres host from --url. Pass the REAL dev-branch "
            "connection string in quotes."
        )

    forbidden = _forbidden_hosts()
    if host in forbidden:
        sys.exit(
            f"REFUSING: '{host}' is a configured PRODUCTION database — this tool is dev-only.\n"
            "Point --url at a separate dev/staging branch with a DIFFERENT host."
        )

    print(f"Target database host: {host}")
    if not args.yes:
        sys.exit("Aborted. Re-run with --yes once you've confirmed this is a DEV/staging DB.")

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(
                "UPDATE employees SET base_salary = NULL, base_salary_enc = NULL "
                "WHERE base_salary IS NOT NULL OR base_salary_enc IS NOT NULL"
            ))
            print(f"Scrubbed salary fields on {result.rowcount} employee row(s).")
    except OperationalError as exc:
        sys.exit(
            f"\nCould not connect to '{host}'.\n"
            "Check that this is your REAL Neon dev-branch string — correct host, "
            "username, password, and ?sslmode=require. Get it from the Neon console.\n"
            f"(driver said: {exc.orig})"
        )
    print("Done — no real salary data remains in this database.")


if __name__ == "__main__":
    main()
