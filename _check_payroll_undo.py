"""Throwaway verification for the payroll undo/reopen work. Safe to delete."""
from sqlalchemy import inspect

from app.db.database import engine
import app.main as m

cols = {c["name"] for c in inspect(engine).get_columns("payroll_runs")}
print("payroll_runs audit columns:", sorted(c for c in cols if "final" in c or "reopen" in c), flush=True)

for r in m.app.routes:
    print("  ", type(r).__name__, repr(r)[:110], flush=True)
