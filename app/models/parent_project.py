from sqlalchemy import Column, Integer, String, Date, Text, TIMESTAMP, ForeignKey, JSON
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from app.db.database import Base


class MainProject(Base):
    """
    Main Project entity for hierarchical project management.
    Hierarchy: MainProject → SubProject → DailySheet → Allocations
    """
    __tablename__ = "main_projects"

    id = Column(Integer, primary_key=True, index=True)
    
    # Core fields
    name = Column(Text, nullable=False)
    program_manager_id = Column(Integer, nullable=True)
    # Multiple PMs per project; program_manager_id stays as the primary PM for backward compat
    program_manager_ids = Column(JSON, nullable=True, default=list)
    description = Column(Text, nullable=True)
    # Organization name (formerly "client")
    client = Column(Text, nullable=True)
    project_type = Column(Text, nullable=False, default="Full")
    
    # Timeline
    global_start_date = Column(Date, nullable=False)
    tentative_duration_months = Column(Integer, nullable=True)
    
    # Status: active, completed, archived
    status = Column(Text, default="active")
    
    # Timestamps
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(
        TIMESTAMP,
        server_default=func.now(),
        onupdate=func.now()
    )
    
    # Relationship to new SubProject (intermediate level)
    sub_projects = relationship(
        "SubProject",
        back_populates="main_project",
        foreign_keys="SubProject.main_project_id"
    )

# Alias for backward compatibility
ParentProject = MainProject
