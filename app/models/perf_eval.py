"""
Project-based monthly performance evaluation.

- PerfProjectParams: legacy per-project parameter template. No longer used
  (the parameters are now hardcoded — see app/constants/perf_params.py). Kept so
  the existing table is left untouched.
- PerfEvaluation: one monthly evaluation per (project, employee, period).
  The employee rates each of the five fixed parameters 1-5 (parameter_values),
  gives an optional overall comment, and submits (status="submitted"). The PM
  then reviews: approves/rejects each parameter, assigns their own 1-5 rating,
  leaves feedback on rejected parameters, and may suggest a bonus
  (status="reviewed"). PM ratings are authoritative (overall_rating).
"""
from sqlalchemy import Column, Integer, Float, String, Text, Boolean, TIMESTAMP, JSON, UniqueConstraint
from sqlalchemy.sql import func

from app.db.database import Base


class PerfProjectParams(Base):
    __tablename__ = "perf_project_params"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, nullable=False, unique=True, index=True)  # daily_sheets.id
    # List of parameter names — legacy, no longer written to.
    params = Column(JSON, nullable=False, default=list)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())


class PerfEvaluation(Base):
    __tablename__ = "perf_evaluations"

    id = Column(Integer, primary_key=True, index=True)

    project_id = Column(Integer, nullable=False, index=True)   # daily_sheets.id
    employee_id = Column(Integer, nullable=False, index=True)
    period = Column(String(7), nullable=False, index=True)      # "YYYY-MM"

    # One object per fixed parameter:
    # {"name": "Quality of Work", "employee_rating": 4,
    #  "pm_rating": 3, "approved": true, "feedback": null}
    parameter_values = Column(JSON, nullable=False, default=list)

    overall_comment = Column(Text, nullable=True)  # optional employee remark

    employee_overall_rating = Column(Float, nullable=True)  # mean of employee ratings (at submit)
    overall_rating = Column(Float, nullable=True)           # mean of PM ratings (at review) — authoritative

    # PM may suggest this employee for a bonus.
    bonus_suggested = Column(Boolean, nullable=False, default=False, server_default="0")
    bonus_note = Column(Text, nullable=True)

    status = Column(String(16), nullable=False, default="submitted")  # submitted | reviewed
    submitted_by = Column(Integer, nullable=True)  # user.id of employee
    reviewed_by = Column(Integer, nullable=True)   # user.id of PM who reviewed

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "employee_id", "period", name="uq_perf_eval_proj_emp_period"),
    )
