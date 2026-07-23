"""
DailySheet model (formerly SubProject).
Represents daily sheets / batches with tasks, time targets, and employee assignments.
Hierarchy: MainProject → SubProject → DailySheet → Allocations
"""
from sqlalchemy import Column, Integer, String, Date, Float, Text, TIMESTAMP, JSON, Boolean, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from app.db.database import Base


class DailySheet(Base):
    __tablename__ = "daily_sheets"

    id = Column(Integer, primary_key=True, index=True)
    
    # === Hierarchy ===
    sub_project_id = Column(Integer, ForeignKey("sub_projects.id"), nullable=True)
    main_project_id = Column(Integer, ForeignKey("main_projects.id"), nullable=True)
    batch_name = Column(Text, nullable=True)
    is_sub_project = Column(Boolean, default=False)
    previous_daily_sheet_id = Column(Integer, nullable=True)
    
    # Relationships
    sub_project = relationship("SubProject", back_populates="daily_sheets", foreign_keys=[sub_project_id])
    main_project = relationship("MainProject", foreign_keys=[main_project_id])

    # === Core Fields ===
    name = Column(Text, nullable=False)
    client = Column(Text, nullable=False)
    project_type = Column(Text, nullable=False)

    total_tasks = Column(Integer, nullable=False)
    estimated_time_per_task = Column(Float, nullable=False)   # annotation time per task (hours)
    review_time_per_task = Column(Float, nullable=True)       # reviewer time per task (hours)
    gearing_ratio = Column(Float, nullable=True)              # informational (e.g. 3, 3.1)

    required_expertise = Column(JSON, nullable=True)
    assigned_employee_ids = Column(JSON, nullable=True, default=[])

    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)  # optional: open-ended sub-projects have no end date

    daily_target = Column(Integer, default=0)
    project_duration_weeks = Column(Integer, nullable=True)
    project_duration_days = Column(Integer, nullable=True)

    required_manpower = Column(Integer, default=0)
    allocated_employees = Column(Integer, default=0)

    is_annotation = Column(Boolean, default=False, nullable=True)
    # Project type classification: { category: subtype }, e.g.
    # {"Data Modalities": "Video", "Annotation Types (By Data)": "Classification"}
    project_types = Column(JSON, nullable=True, default=dict)
    priority = Column(Text, default="medium")
    project_status = Column(Text, default="active")

    # Encord integration + PM sentiment (one daily-sheet = one Encord project)
    encord_project_hash = Column(Text, nullable=True, index=True)
    sentiment = Column(Text, nullable=True)  # GOOD | AVG | Poor

    # Team composition (manual, informational). required_manpower is auto-computed
    # as autonex_annotators + autonex_reviewers + qc_count.
    annotators_total = Column(Integer, default=0)
    workforce_vendors = Column(JSON, nullable=True, default=list)   # list of vendor names
    autonex_annotators = Column(Integer, default=0)
    autonex_reviewers = Column(Integer, default=0)
    workforce_reviewers = Column(Integer, default=0)
    qc_count = Column(Integer, default=0)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(
        TIMESTAMP,
        server_default=func.now(),
        onupdate=func.now()
    )

# Backward compatibility aliases
SubProject = DailySheet
Project = DailySheet