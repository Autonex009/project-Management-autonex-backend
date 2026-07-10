"""
Project-based monthly self-evaluation.

- PerfProjectParams: the evaluation parameters (names) a PM defines for a project.
  Shared by all employees allocated to that project.
- PerfEvaluation: one monthly self-evaluation per (project, employee, period).
  The employee fills a value/note for each parameter, three fixed reflection
  fields, and one overall 1–5 star rating. Once submitted it is locked; the PM
  reviews it (accept / edit).
"""
from sqlalchemy import Column, Integer, Float, String, Text, TIMESTAMP, JSON, UniqueConstraint
from sqlalchemy.sql import func

from app.db.database import Base


class PerfProjectParams(Base):
    __tablename__ = "perf_project_params"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, nullable=False, unique=True, index=True)  # daily_sheets.id
    # List of parameter names, e.g. ["Time per task", "Quality", "Rejection Rate"]
    params = Column(JSON, nullable=False, default=list)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())


class PerfEvaluation(Base):
    __tablename__ = "perf_evaluations"

    id = Column(Integer, primary_key=True, index=True)

    project_id = Column(Integer, nullable=False, index=True)   # daily_sheets.id
    employee_id = Column(Integer, nullable=False, index=True)
    period = Column(String(7), nullable=False, index=True)      # "YYYY-MM"

    # [{"name": "Quality", "value": "…"}, …] — value is free text/number/notes
    parameter_values = Column(JSON, nullable=False, default=list)

    # Three fixed reflection fields
    contributions = Column(Text, nullable=True)   # Overall contributions in last month
    strengths = Column(Text, nullable=True)       # Areas that are your strengths
    improvements = Column(Text, nullable=True)     # Areas to improve

    overall_rating = Column(Float, nullable=True)  # 1–5

    status = Column(String(16), nullable=False, default="submitted")  # submitted | accepted
    submitted_by = Column(Integer, nullable=True)  # user.id of employee
    reviewed_by = Column(Integer, nullable=True)   # user.id of PM who accepted/edited

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "employee_id", "period", name="uq_perf_eval_proj_emp_period"),
    )
