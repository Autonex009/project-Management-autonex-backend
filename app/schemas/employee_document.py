from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from enum import Enum


class DocTypeEnum(str, Enum):
    internship_offer_letter = "internship_offer_letter"
    fulltime_offer_letter = "fulltime_offer_letter"
    internship_completion_certificate = "internship_completion_certificate"
    experience_letter = "experience_letter"
    salary_structure = "salary_structure"
    org_policy = "org_policy"


class GenerateDocumentRequest(BaseModel):
    doc_type: str
    dynamic_data: Optional[dict] = None  # HR-entered fields (e.g. stipend, ctc, variable)


class EmployeeDocumentResponse(BaseModel):
    id: int
    employee_id: int
    doc_type: str
    file_url: Optional[str] = None
    file_name: Optional[str] = None
    version: int
    source: str
    uploaded_by: Optional[int] = None
    is_active: bool
    generated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    # Enriched fields
    uploaded_by_name: Optional[str] = None
    download_url: Optional[str] = None  # Signed URL for download

    class Config:
        from_attributes = True


class EmployeeDocumentsListResponse(BaseModel):
    employee_id: int
    employee_name: Optional[str] = None
    total: int
    documents: List[EmployeeDocumentResponse]


DOC_TYPE_LABELS = {
    "internship_offer_letter": "Internship Offer Letter",
    "fulltime_offer_letter": "Full-time Offer Letter",
    "internship_completion_certificate": "Completion Certificate",
    "experience_letter": "Experience Letter",
    "salary_structure": "Salary Structure",
    "org_policy": "Org Policy",
}

# Which doc types apply to which employee types
DOC_TYPE_APPLICABILITY = {
    "Intern": [
        "internship_offer_letter",
        "salary_structure",
        "org_policy",
        "internship_completion_certificate",
        "experience_letter",
    ],
    "Full-time": [
        "fulltime_offer_letter",
        "salary_structure",
        "org_policy",
        "experience_letter",
    ],
    "Part-time": [
        "fulltime_offer_letter",
        "salary_structure",
        "org_policy",
    ],
    "Contract": [
        "org_policy",
        "experience_letter",
    ],
}
