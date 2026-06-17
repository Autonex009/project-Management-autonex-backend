"""
CompanySetting model - Stores admin-editable key-value configuration
(e.g. WiFi credentials, office details) for the Company Information page.
"""
from sqlalchemy import Column, Integer, Text, TIMESTAMP
from sqlalchemy.sql import func

from app.db.database import Base


class CompanySetting(Base):
    __tablename__ = "company_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(Text, unique=True, nullable=False, index=True)
    value = Column(Text, nullable=True)
    updated_by = Column(Integer, nullable=True)
    updated_at = Column(
        TIMESTAMP,
        server_default=func.now(),
        onupdate=func.now(),
    )
