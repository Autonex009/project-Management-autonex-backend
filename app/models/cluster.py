from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Date,
    TIMESTAMP,
    ForeignKey,
    Index,
    CheckConstraint,
    Boolean,
    JSON,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from app.db.database import Base


JSONB_TYPE = JSON().with_variant(JSONB(), "postgresql")


class Cluster(Base):
    """
    A Cluster is a named cohort of employees.
    Cohorts can be manual ('custom') or dynamic based on dynamic_scope.
    """
    __tablename__ = "clusters"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # Dynamic Smart Cluster Configuration
    # If dynamic_scope == 'custom', membership is managed manually via cluster_members.
    # Otherwise, membership is automatically evaluated.
    dynamic_scope = Column(
        String(64),
        nullable=False,
        default="custom",
        server_default="custom",
    )  # custom, everyone, full_time, annotators_reviewers, developers, project_managers, team_leads, new_joiners

    # Optional multi-rule auto-membership list (OR-union of non-custom scopes);
    # dynamic_scope mirrors the primary scope so existing CHECK constraints & partial index stay valid.
    dynamic_scopes = Column(JSONB_TYPE, nullable=True, default=None)

    # Required when dynamic_scope = 'new_joiners' or 'new_joiners' is in dynamic_scopes
    joined_on_or_after = Column(Date, nullable=True)

    is_archived = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    members = relationship("ClusterMember", back_populates="cluster", cascade="all, delete-orphan")
    assignments = relationship("Assignment", back_populates="cluster")

    @property
    def effective_scopes(self) -> list[str]:
        """Normalized active scope list combining multi-rule JSON and legacy single dynamic_scope."""
        if isinstance(self.dynamic_scopes, list):
            cleaned = [str(s) for s in self.dynamic_scopes if s and s != "custom"]
            if cleaned:
                return list(dict.fromkeys(cleaned))
        if self.dynamic_scope and self.dynamic_scope != "custom":
            return [self.dynamic_scope]
        return ["custom"]

    @property
    def is_dynamic(self) -> bool:
        return self.dynamic_scope != "custom" or any(s != "custom" for s in self.effective_scopes)

    __table_args__ = (
        CheckConstraint(
            "dynamic_scope IN ('custom', 'everyone', 'full_time', 'annotators_reviewers', 'developers', 'project_managers', 'team_leads', 'new_joiners')",
            name="chk_clusters_dynamic_scope",
        ),
        CheckConstraint(
            "dynamic_scope <> 'new_joiners' OR joined_on_or_after IS NOT NULL",
            name="chk_new_joiners_date",
        ),
        Index(
            "uq_clusters_name_active",
            "name",
            unique=True,
            postgresql_where=(is_archived == False),
            sqlite_where=(is_archived == False),
        ),
        Index(
            "ix_clusters_dynamic_active",
            "dynamic_scope",
            postgresql_where=((dynamic_scope != "custom") & (is_archived == False)),
            sqlite_where=((dynamic_scope != "custom") & (is_archived == False)),
        ),
    )


class ClusterMember(Base):
    """
    Cluster membership junction table with composite natural primary key (cluster_id, employee_id).
    """
    __tablename__ = "cluster_members"

    cluster_id = Column(Integer, ForeignKey("clusters.id", ondelete="CASCADE"), primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True, index=True)
    added_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    # Relationships
    cluster = relationship("Cluster", back_populates="members")
    employee = relationship("Employee")
