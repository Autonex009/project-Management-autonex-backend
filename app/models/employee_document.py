from sqlalchemy import Column, Integer, String, Text, Boolean, TIMESTAMP, ForeignKey, Enum as SAEnum
from sqlalchemy.sql import func
import enum

from app.db.database import Base


class DocType(str, enum.Enum):
    internship_offer_letter = "internship_offer_letter"
    fulltime_offer_letter = "fulltime_offer_letter"
    internship_completion_certificate = "internship_completion_certificate"
    experience_letter = "experience_letter"
    salary_structure = "salary_structure"
    org_policy = "org_policy"


class EmployeeDocument(Base):
    __tablename__ = "employee_documents"

    id = Column(Integer, primary_key=True, index=True)

    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True)

    # Type of document
    doc_type = Column(
        SAEnum(
            "internship_offer_letter",
            "fulltime_offer_letter",
            "internship_completion_certificate",
            "experience_letter",
            "salary_structure",
            "org_policy",
            name="doc_type_enum",
        ),
        nullable=False,
    )

    # Storage URL (Supabase path)
    file_url = Column(Text, nullable=True)
    file_name = Column(Text, nullable=True)

    # Versioning: 1, 2, 3 incremented on each regeneration
    version = Column(Integer, nullable=False, default=1)

    # Source: "generated" (auto-created by template engine) or "uploaded" (manual HR upload)
    source = Column(String(32), nullable=False, default="generated")

    # Who triggered the upload/generation (user.id)
    uploaded_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Soft delete — older versions keep their row
    is_active = Column(Boolean, nullable=False, default=True)

    generated_at = Column(TIMESTAMP, server_default=func.now())
    created_at = Column(TIMESTAMP, server_default=func.now())
