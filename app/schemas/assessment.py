from datetime import datetime
import json
from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator


MAX_TELEMETRY_EVENT_DETAILS_BYTES = 2048
MAX_TELEMETRY_EVENTS_PER_BATCH = 50


def _validate_event_details_size(v: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(v, dict):
        raise ValueError("Telemetry details must be a JSON object.")
    encoded = json.dumps(v, separators=(",", ":"), default=str).encode("utf-8")
    if len(encoded) > MAX_TELEMETRY_EVENT_DETAILS_BYTES:
        raise ValueError(
            f"Telemetry event details exceed {MAX_TELEMETRY_EVENT_DETAILS_BYTES} bytes."
        )
    return v


TestType = Literal["mcq", "typing"]
AssessmentStatus = Literal["draft", "published", "archived"]
LogEventType = Literal[
    "tab_switch",
    "window_blur",
    "fullscreen_exit",
    "camera_lost",
    "multiple_faces",
    "no_face",
]


def _validate_proctoring_config_dict(cfg: Optional[Dict[str, Any]]) -> None:
    if cfg is None:
        return
    if "require_camera" in cfg and not isinstance(cfg["require_camera"], bool):
        raise ValueError("proctoring_config.require_camera must be a boolean.")
    if "max_tab_switches" in cfg and cfg["max_tab_switches"] is not None:
        val = cfg["max_tab_switches"]
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise ValueError("proctoring_config.max_tab_switches must be a non-negative integer.")


class AssessmentCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    test_type: TestType = "mcq"
    duration_minutes: int = Field(20, ge=1, le=300)
    passing_score: Optional[float] = Field(None, ge=0.0, le=100.0)
    max_attempts: int = Field(1, ge=1, le=20)
    test_config: Dict[str, Any] = Field(default_factory=dict)
    proctoring_config: Dict[str, Any] = Field(
        default_factory=lambda: {"require_camera": True, "max_tab_switches": 3}
    )

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, v: str) -> str:
        if isinstance(v, str):
            v = v.strip()
        return v

    @model_validator(mode="after")
    def validate_proctoring(self):
        _validate_proctoring_config_dict(self.proctoring_config)
        return self


class AssessmentUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    status: Optional[AssessmentStatus] = None
    duration_minutes: Optional[int] = Field(None, ge=1, le=300)
    passing_score: Optional[float] = Field(None, ge=0.0, le=100.0)
    max_attempts: Optional[int] = Field(None, ge=1, le=20)
    test_config: Optional[Dict[str, Any]] = None
    proctoring_config: Optional[Dict[str, Any]] = None

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, v: Optional[str]) -> Optional[str]:
        if isinstance(v, str):
            v = v.strip()
        return v

    @model_validator(mode="after")
    def validate_proctoring(self):
        _validate_proctoring_config_dict(self.proctoring_config)
        return self


class AssessmentRestore(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    restore_to: Literal["draft", "published"] = "draft"
    restore_assignments: bool = False

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, v: Optional[str]) -> Optional[str]:
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v


class AssessmentOverviewResponse(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    test_type: str
    duration_minutes: int
    passing_score: Optional[float] = None
    max_attempts: int
    proctoring_config: Dict[str, Any]
    status: str

    model_config = ConfigDict(from_attributes=True)


class AssessmentListResponse(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    test_type: str
    status: str
    version: int
    duration_minutes: int
    passing_score: Optional[float] = None
    max_attempts: int
    proctoring_config: Dict[str, Any]
    created_at: Optional[datetime] = None
    # Only populated when the list is requested with include_config=true
    test_config: Optional[Dict[str, Any]] = None
    categories_count: Optional[int] = None
    questions_count: Optional[int] = None
    pool_size: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class AssessmentDetailResponse(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    test_type: str
    status: str
    version: int
    duration_minutes: int
    passing_score: Optional[float] = None
    max_attempts: int
    test_config: Dict[str, Any]
    proctoring_config: Dict[str, Any]
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class StartAttemptRequest(BaseModel):
    camera_verified: bool = False


class TelemetryEventItem(BaseModel):
    event_type: LogEventType
    details: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("details")
    @classmethod
    def check_details_size(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        return _validate_event_details_size(v)


class TelemetryIngestRequest(BaseModel):
    events: List[TelemetryEventItem] = Field(..., min_length=1, max_length=MAX_TELEMETRY_EVENTS_PER_BATCH)


class DraftAutosaveRequest(BaseModel):
    answers: Dict[str, Any] = Field(default_factory=dict)
    answers_seq: int = Field(0, ge=0)
    events: Optional[List[TelemetryEventItem]] = Field(None, max_length=MAX_TELEMETRY_EVENTS_PER_BATCH)


class SubmitAttemptRequest(BaseModel):
    answers: Dict[str, Any] = Field(default_factory=dict)


class GrantRetestRequest(BaseModel):
    reason: str = Field(..., min_length=1)
    new_deadline: Optional[datetime] = None


class CandidateDeadlineExtendRequest(BaseModel):
    new_deadline: datetime
    reason: Optional[str] = "Approved individual extension"


class CandidateAttemptResponse(BaseModel):
    id: int
    enrollment_id: int
    attempt_number: int
    assessment_version: int
    status: str
    question_snapshot: Dict[str, Any]
    started_at: datetime
    expires_at: datetime
    answers: Optional[Dict[str, Any]] = None
    answers_seq: int = 0
    camera_verified: bool = False

    model_config = ConfigDict(from_attributes=True)


class AssessmentLogResponse(BaseModel):
    id: int
    attempt_id: int
    event_type: str
    event_time: Optional[datetime] = None
    details: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(from_attributes=True)


class CandidateAttemptResultResponse(BaseModel):
    """Candidate-facing attempt result: strips anti-cheat flag hints and diagnostics."""
    id: int
    attempt_number: int
    status: str
    score: Optional[float] = None
    passed: Optional[bool] = None
    time_spent_seconds: int = 0
    result_summary: Optional[Dict[str, Any]] = None
    started_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class AttemptResultResponse(BaseModel):
    """Evaluator-facing attempt result: includes proctoring flag status & diagnostics."""
    id: int
    attempt_number: int
    status: str
    score: Optional[float] = None
    passed: Optional[bool] = None
    time_spent_seconds: int = 0
    camera_verified: bool = False
    is_flagged: bool = False
    flag_reason: Optional[str] = None
    tab_switch_count: int = 0
    window_blur_count: int = 0
    camera_lost_count: int = 0
    result_summary: Optional[Dict[str, Any]] = None
    started_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    recording_status: Optional[str] = None
    logs: List[AssessmentLogResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class CandidateScorecardResponse(BaseModel):
    enrollment_id: int
    assignment_id: Optional[int] = None
    assessment_id: int
    assessment_title: str
    test_type: str = "mcq"
    duration_minutes: int = 20
    passing_score: Optional[float] = None
    question_count: Optional[int] = None  # questions per sitting (MCQ); None for typing
    employee_id: int
    employee_name: Optional[str] = None
    status: str
    deadline: Optional[datetime] = None
    attempts_used: int = 0
    max_attempts: int = 1
    extra_attempts: int = 0
    best_score: Optional[float] = None
    attempts: List[CandidateAttemptResultResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class ScorecardResponse(BaseModel):
    enrollment_id: int
    assignment_id: Optional[int] = None
    assessment_id: int
    assessment_title: str
    test_type: str = "mcq"
    duration_minutes: int = 20
    passing_score: Optional[float] = None
    employee_id: int
    employee_name: Optional[str] = None
    status: str
    deadline: Optional[datetime] = None
    attempts_used: int = 0
    max_attempts: int = 1
    extra_attempts: int = 0
    best_score: Optional[float] = None
    tab_switch_count: int = 0
    is_flagged: bool = False
    cluster_name: Optional[str] = None
    assignment_created_at: Optional[datetime] = None
    employee_designation: Optional[str] = None
    attempts: List[AttemptResultResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class ManualReminderRequest(BaseModel):
    cluster_id: Optional[int] = None
    assessment_id: Optional[int] = None
    enrollment_id: Optional[int] = None
    enrollment_ids: Optional[List[int]] = Field(None, min_length=1, max_length=500)
    send_all_pending: bool = False

    @model_validator(mode="after")
    def require_explicit_scope(self):
        # An empty payload must never fan out to every pending candidate in the company.
        if not (
            self.cluster_id is not None
            or self.assessment_id is not None
            or self.enrollment_id is not None
            or self.enrollment_ids
            or self.send_all_pending
        ):
            raise ValueError(
                "Provide cluster_id, assessment_id, enrollment_id(s) or send_all_pending=true."
            )
        return self
