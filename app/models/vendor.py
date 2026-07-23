from sqlalchemy import Column, Integer, String, TIMESTAMP
from sqlalchemy.sql import func

from app.db.database import Base


class Vendor(Base):
    """Workforce vendor (e.g. an outsourcing partner). Persisted so vendors
    created on one project are reusable across all projects."""
    __tablename__ = "vendors"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
