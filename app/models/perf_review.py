from sqlalchemy import Column, ForeignKey, Integer, Text, Float, String, TIMESTAMP, JSON, UniqueConstraint
from sqlalchemy.sql import func

from app.db.database import Base


class PerfReview(Base):
    """Monthly structured performance review.

    One review per employee per month (period = "YYYY-MM"). Each review scores
    five fixed criteria on a 1–5 scale; the average is computed and stored.
    """
    __tablename__ = "perf_reviews"

    id = Column(Integer, primary_key=True, index=True)

    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    reviewer_id = Column(Integer, nullable=True)          # user.id of the PM/admin who wrote it
    reviewer_role = Column(String(16), nullable=True)     # "pm" | "admin"

    # {"quality":4,"productivity":3,"communication":5,"teamwork":4,"initiative":3}
    criteria_ratings = Column(JSON, nullable=False)
    average = Column(Float, nullable=True)

    comment = Column(Text, nullable=True)                 # optional overall comment
    period = Column(String(7), nullable=False, index=True)  # "YYYY-MM"

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("employee_id", "period", name="uq_perf_reviews_employee_period"),
    )
