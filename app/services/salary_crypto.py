"""
Salary field encryption (at rest).
==================================
`employees.base_salary` is stored ENCRYPTED (Fernet symmetric ciphertext) in the
column `base_salary_enc`; the plaintext column is never populated. The decryption
key lives ONLY in the `SALARY_KEY` environment variable, which is set on the
production deployment as a sensitive secret — never in the repo and never in dev
environments.

Consequences (the actual protection):
  • A developer with the database connection string sees only ciphertext.
  • A developer running the app locally has no SALARY_KEY → cannot decrypt.
  • Plaintext salary exists only in memory, only inside the admin payroll path,
    only on the production deployment that holds the key.

Generate a key with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

If SALARY_KEY is unset/invalid, encryption is DISABLED: writes are dropped
(salary stays unset) and reads return None. This is intentional — dev
environments carry no real salaries, and prod must have the key configured.
"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def _get_fernet():
    """Build a Fernet from SALARY_KEY, or None if unset/invalid.

    Not cached: reads the env each call so key rotation / tests take effect
    immediately. The cost is negligible at payroll scale.
    """
    key = (os.getenv("SALARY_KEY") or "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except Exception as exc:  # malformed key
        logger.error("Invalid SALARY_KEY — salary encryption disabled: %s", exc)
        return None


def encryption_enabled() -> bool:
    return _get_fernet() is not None


def encrypt_salary(value) -> Optional[str]:
    """Encrypt a numeric salary to a ciphertext string. None/no-key → None."""
    if value is None:
        return None
    fernet = _get_fernet()
    if fernet is None:
        logger.warning("SALARY_KEY not configured — salary write ignored (not stored).")
        return None
    return fernet.encrypt(str(float(value)).encode()).decode()


def decrypt_salary(token) -> Optional[float]:
    """Decrypt a ciphertext string back to a float. Missing/no-key/invalid → None."""
    if not token:
        return None
    fernet = _get_fernet()
    if fernet is None:
        return None
    try:
        return float(fernet.decrypt(token.encode()).decode())
    except Exception as exc:
        logger.warning("Failed to decrypt a salary value (wrong/rotated key?): %s", exc)
        return None
