from sqlalchemy import Column, Integer, Text, JSON, TIMESTAMP, ForeignKey
from sqlalchemy.sql import func

from app.db.database import Base


class Admin(Base):
    __tablename__ = "admins"

    id = Column(Integer, primary_key=True, index=True)

    # Link back to the auth user
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)

    name = Column(Text, nullable=False)
    email = Column(Text, nullable=False, unique=True)

    status = Column(Text, default="active")  # active, inactive

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())
