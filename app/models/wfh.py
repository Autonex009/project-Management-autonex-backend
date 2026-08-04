from sqlalchemy import Boolean, Column, Integer, String, Date, Text, TIMESTAMP
from sqlalchemy.sql import func
from app.db.database import Base


class WFHRequest(Base):
    __tablename__ = "wfh_requests"

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, nullable=False, index=True)
    wfh_date = Column(Date, nullable=False, index=True)   # start date
    end_date = Column(Date, nullable=True)                 # end date (defaults to wfh_date for single-day)
    reason = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="pending")  # pending, approved, rejected
    approved_by = Column(Integer, nullable=True)
    remark = Column(Text, nullable=True)
    flagged = Column(Boolean, default=False, nullable=False)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())
