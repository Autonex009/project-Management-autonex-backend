"""
SQLAlchemy models for Onboarding.
"""
from sqlalchemy import Column, Integer, String, Text, Boolean, ForeignKey, TIMESTAMP, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.database import Base


class OnboardingModule(Base):
    __tablename__ = "onboarding_modules"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=False)
    status = Column(String(20), default="DRAFT")  # 'DRAFT' or 'PUBLISHED'
    assessment_url = Column(Text, nullable=True)
    order = Column(Integer, default=0)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    sections = relationship("OnboardingSection", back_populates="module", cascade="all, delete-orphan")


class OnboardingSection(Base):
    __tablename__ = "onboarding_sections"

    id = Column(Integer, primary_key=True, index=True)
    module_id = Column(Integer, ForeignKey("onboarding_modules.id", ondelete="CASCADE"), nullable=False)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=False)
    video_url = Column(Text, nullable=True)
    video_duration = Column(String(50), nullable=True)
    quiz_passing_score = Column(Integer, default=0)
    order = Column(Integer, default=0)

    module = relationship("OnboardingModule", back_populates="sections")
    documents = relationship("OnboardingDocument", back_populates="section", cascade="all, delete-orphan")
    questions = relationship("OnboardingQuizQuestion", back_populates="section", cascade="all, delete-orphan")


class OnboardingDocument(Base):
    __tablename__ = "onboarding_documents"

    id = Column(Integer, primary_key=True, index=True)
    section_id = Column(Integer, ForeignKey("onboarding_sections.id", ondelete="CASCADE"), nullable=False)
    title = Column(Text, nullable=False)
    type = Column(String(50), nullable=False)
    url = Column(Text, nullable=False)

    section = relationship("OnboardingSection", back_populates="documents")


class OnboardingQuizQuestion(Base):
    __tablename__ = "onboarding_quiz_questions"

    id = Column(Integer, primary_key=True, index=True)
    section_id = Column(Integer, ForeignKey("onboarding_sections.id", ondelete="CASCADE"), nullable=False)
    question = Column(Text, nullable=False)
    options = Column(JSON, nullable=False)  # List of strings (JSON array)
    correct_option_index = Column(Integer, nullable=False)

    section = relationship("OnboardingSection", back_populates="questions")


class OnboardingProgress(Base):
    __tablename__ = "onboarding_progress"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    module_id = Column(Integer, ForeignKey("onboarding_modules.id", ondelete="CASCADE"), nullable=False)
    section_id = Column(Integer, ForeignKey("onboarding_sections.id", ondelete="CASCADE"), nullable=False)
    completed_at = Column(TIMESTAMP, server_default=func.now())


class OnboardingQuizAttempt(Base):
    __tablename__ = "onboarding_quiz_attempts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    question_id = Column(Integer, ForeignKey("onboarding_quiz_questions.id", ondelete="CASCADE"), nullable=False)
    section_id = Column(Integer, ForeignKey("onboarding_sections.id", ondelete="CASCADE"), nullable=False)
    chosen_index = Column(Integer, nullable=False)
    is_correct = Column(Boolean, nullable=False)
    attempted_at = Column(TIMESTAMP, server_default=func.now())


class OnboardingTeamMember(Base):
    __tablename__ = "onboarding_team_members"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(Text, nullable=False)
    role = Column(Text, nullable=False)
    department = Column(Text, nullable=False)
    email = Column(Text, nullable=True)
    linkedin = Column(Text, nullable=True)
    slack = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
