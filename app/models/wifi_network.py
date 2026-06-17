"""
WifiNetwork model - Stores multiple office WiFi credentials,
editable by admins and visible to all employees.
"""
from sqlalchemy import Column, Integer, Text, TIMESTAMP
from sqlalchemy.sql import func

from app.db.database import Base


class WifiNetwork(Base):
    __tablename__ = "wifi_networks"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(Text, nullable=False)
    password = Column(Text, nullable=True)
    updated_by = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(
        TIMESTAMP,
        server_default=func.now(),
        onupdate=func.now(),
    )
