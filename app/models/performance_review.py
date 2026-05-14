from sqlalchemy import Column, ForeignKey, Integer, Text, Float, TIMESTAMP
from sqlalchemy.sql import func

from app.db.database import Base


class PerformanceReview(Base):
    __tablename__ = "performance_reviews"

    id = Column(Integer, primary_key=True, index=True)

    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    reviewer_id = Column(Integer, nullable=True)  # user.id of the PM who wrote the review

    # "feedback" | "performance_review" | "comment"
    review_type = Column(Text, nullable=False, default="feedback")

    title = Column(Text, nullable=False)
    content = Column(Text, nullable=False)

    # Optional 1–5 star rating (used mainly for "performance_review" type)
    rating = Column(Float, nullable=True)

    # e.g. "Q1 2025", "May 2025" — free-form period label
    period = Column(Text, nullable=True)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(
        TIMESTAMP,
        server_default=func.now(),
        onupdate=func.now(),
    )
