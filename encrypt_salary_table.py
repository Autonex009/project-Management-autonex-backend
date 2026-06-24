"""
One-time migration: encrypt the `salary` table's pay columns at rest.
==================================================================
After this runs, a raw `SELECT` on the salary table shows only Fernet ciphertext;
only the backend (holding SALARY_KEY) can decrypt. Safe to re-run — values that
are already encrypted are skipped.

Usage:
    python encrypt_salary_table.py            # DRY RUN — reports what would change
    python encrypt_salary_table.py --apply    # actually encrypt and commit

Requires SALARY_KEY to be set (in .env). The SAME key must be configured on every
deployment that reads this DB, or those apps won't be able to decrypt.
"""
import sys

from app.db.database import SessionLocal
from app.models.payroll import Salary
from app.services.salary_crypto import encrypt_salary, decrypt_salary, encryption_enabled
from app.api.payroll import _parse_money

PAY_COLUMNS = ["base_pay_annual", "optional_bonus_annual", "base_pay_monthly", "opt_bonus_monthly"]


def main(apply: bool):
    if not encryption_enabled():
        print("ERROR: SALARY_KEY is not configured — cannot encrypt. Aborting.")
        sys.exit(1)

    db = SessionLocal()
    rows = db.query(Salary).order_by(Salary.id).all()
    to_encrypt = 0
    already = 0
    skipped_unparseable = 0

    for row in rows:
        for col in PAY_COLUMNS:
            stored = getattr(row, col)
            if not stored:
                continue
            # Already ciphertext? decrypt succeeds -> skip (idempotent).
            if decrypt_salary(stored) is not None:
                already += 1
                continue
            amount = _parse_money(stored)
            if amount is None:
                skipped_unparseable += 1
                continue
            to_encrypt += 1
            if apply:
                setattr(row, col, encrypt_salary(amount))

    if apply:
        db.commit()
        print(f"APPLIED: encrypted {to_encrypt} value(s); {already} already encrypted; "
              f"{skipped_unparseable} unparseable left as-is.")
    else:
        print(f"DRY RUN: would encrypt {to_encrypt} value(s); {already} already encrypted; "
              f"{skipped_unparseable} unparseable would be left as-is.")
        print("Re-run with --apply to commit.")
    db.close()


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
