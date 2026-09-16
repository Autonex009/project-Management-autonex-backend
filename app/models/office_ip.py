"""
OfficeIP model - Stores authorized public IP addresses and CIDR blocks for office check-in.
Editable by admins.
"""
from sqlalchemy import Column, Integer, Text, TIMESTAMP
from sqlalchemy.sql import func

from app.db.database import Base


class OfficeIP(Base):
    __tablename__ = "office_ips"

    id = Column(Integer, primary_key=True, index=True)
    ip_address = Column(Text, nullable=False, unique=True)
    label = Column(Text, nullable=True)
    floor = Column(Text, nullable=True)
    updated_by = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(
        TIMESTAMP,
        server_default=func.now(),
        onupdate=func.now(),
    )
