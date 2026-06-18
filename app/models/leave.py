from sqlalchemy import Boolean, Column, Integer, String, Date, Text, TIMESTAMP
from sqlalchemy.sql import func
from app.db.database import Base

class Leave(Base):
    __tablename__ = "leaves"

    id = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, nullable=False)
    leave_type = Column(String)
    start_date = Column(Date)
    end_date = Column(Date)
    reason = Column(Text, nullable=True)
    status = Column(String, default="pending")  # pending, approved, rejected
    approved_by = Column(Integer, nullable=True)  # user_id of approver
    razorpay_applied = Column(Boolean, default=False)
    flagged = Column(Boolean, default=False, nullable=False)
    approval_remark = Column(Text, nullable=True)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())
