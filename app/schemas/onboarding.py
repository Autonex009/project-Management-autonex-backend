from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime


# ── Document Schemas ──────────────────────────────────────────────────────────
class OnboardingDocumentBase(BaseModel):
    title: str
    type: str
    url: str


class OnboardingDocumentCreate(OnboardingDocumentBase):
    pass


class OnboardingDocumentResponse(OnboardingDocumentBase):
    id: int
    section_id: int

    class Config:
        from_attributes = True


# ── Quiz Question Schemas ─────────────────────────────────────────────────────
class OnboardingQuizQuestionBase(BaseModel):
    question: str
    options: List[str]
    correct_option_index: int


class OnboardingQuizQuestionCreate(OnboardingQuizQuestionBase):
    pass


class OnboardingQuizQuestionResponse(OnboardingQuizQuestionBase):
    id: int
    section_id: int

    class Config:
        from_attributes = True


# ── Section Schemas ───────────────────────────────────────────────────────────
class OnboardingSectionBase(BaseModel):
    title: str
    description: str
    video_url: Optional[str] = None
    video_duration: Optional[str] = None
    quiz_passing_score: int = 0
    order: int = 0


class OnboardingSectionCreate(OnboardingSectionBase):
    documents: Optional[List[OnboardingDocumentCreate]] = []
    questions: Optional[List[OnboardingQuizQuestionCreate]] = []


class OnboardingSectionResponse(OnboardingSectionBase):
    id: int
    module_id: int
    documents: List[OnboardingDocumentResponse] = []
    questions: List[OnboardingQuizQuestionResponse] = []

    class Config:
        from_attributes = True


# ── Module Schemas ────────────────────────────────────────────────────────────
class OnboardingModuleBase(BaseModel):
    title: str
    description: str
    status: str = "DRAFT"  # 'DRAFT' or 'PUBLISHED'
    assessment_url: Optional[str] = None
    order: int = 0


class OnboardingModuleCreate(OnboardingModuleBase):
    sections: Optional[List[OnboardingSectionCreate]] = []



class OnboardingModuleResponse(OnboardingModuleBase):
    id: int
    created_at: datetime
    updated_at: datetime
    sections: List[OnboardingSectionResponse] = []

    class Config:
        from_attributes = True


# ── Progress Schemas ──────────────────────────────────────────────────────────
class OnboardingProgressBase(BaseModel):
    module_id: int
    section_id: int


class OnboardingProgressCreate(OnboardingProgressBase):
    user_id: Optional[int] = None


class OnboardingProgressResponse(OnboardingProgressBase):
    id: int
    user_id: int
    completed_at: datetime

    class Config:
        from_attributes = True


# ── Quiz Attempt Schemas ──────────────────────────────────────────────────────
class OnboardingQuizAttemptBase(BaseModel):
    question_id: int
    section_id: int
    chosen_index: int


class OnboardingQuizAttemptCreate(OnboardingQuizAttemptBase):
    user_id: Optional[int] = None


class OnboardingQuizAttemptResponse(OnboardingQuizAttemptBase):
    id: int
    user_id: int
    is_correct: bool
    attempted_at: datetime

    class Config:
        from_attributes = True


# ── Team Member Schemas ────────────────────────────────────────────────────────
class OnboardingTeamMemberBase(BaseModel):
    name: str
    role: str
    department: str
    email: Optional[str] = None
    linkedin: Optional[str] = None
    slack: Optional[str] = None


class OnboardingTeamMemberCreate(OnboardingTeamMemberBase):
    pass


class OnboardingTeamMemberResponse(OnboardingTeamMemberBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True
