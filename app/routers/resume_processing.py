"""Router for Resume Processing settings and Candidate extraction inspection/editing."""

from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, require_hr
from app.llm import get_llm_optional
from app.models import Candidate, Interview, User
from app.resume_parsing import find_email
from app.resume_processor import (
    EXTRACTION_MODES,
    ResumeProcessingService,
    is_llm_available,
    is_tesseract_available,
)

router = APIRouter(tags=["resume processing"])


class ModeSettingIn(BaseModel):
    mode: str = Field(..., description="One of: standard, ocr, ai, hybrid")


class ExtractionPatchIn(BaseModel):
    name: str | None = None
    email: str | None = None
    skills: list[str] | None = None
    education: list[dict[str, Any] | str] | None = None
    experience: list[dict[str, Any] | str] | None = None
    resume_text: str | None = None


class ReprocessIn(BaseModel):
    mode: str | None = None


MODE_DESCRIPTIONS = [
    {
        "id": "standard",
        "label": "Standard PDF Parser",
        "description": "Fast PyMuPDF (fitz) text parser with pypdf fallback.",
    },
    {
        "id": "ocr",
        "label": "OCR (Tesseract)",
        "description": "Optical Character Recognition via Tesseract for scanned/image PDFs.",
    },
    {
        "id": "ai",
        "label": "AI Extraction",
        "description": "LLM-powered extraction with structured entity and profile parsing.",
    },
    {
        "id": "hybrid",
        "label": "Hybrid (Recommended)",
        "description": "Parser -> automatic OCR fallback on low text -> AI structured extraction.",
    },
]


@router.get("/settings/resume-processor")
def get_resume_processing_settings(db: Session = Depends(get_db), _: User = Depends(require_hr)):
    service = ResumeProcessingService(db)
    current_mode = service.get_system_mode()
    return {
        "mode": current_mode,
        "available_modes": MODE_DESCRIPTIONS,
        "tesseract_available": is_tesseract_available(),
        "llm_available": is_llm_available(),
    }


@router.put("/settings/resume-processor")
@router.post("/settings/resume-processor")
def update_resume_processing_settings(
    body: ModeSettingIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_hr),
):
    if body.mode not in EXTRACTION_MODES:
        raise HTTPException(400, f"Invalid mode. Must be one of: {', '.join(EXTRACTION_MODES)}")
    service = ResumeProcessingService(db)
    service.set_system_mode(body.mode, user_id=user.id)
    return {
        "mode": body.mode,
        "available_modes": MODE_DESCRIPTIONS,
        "tesseract_available": is_tesseract_available(),
        "llm_available": is_llm_available(),
    }


@router.get("/candidates/{candidate_id}/extraction")
def get_candidate_extraction(
    candidate_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    cand = db.get(Candidate, candidate_id)
    if not cand:
        raise HTTPException(404, "Candidate not found")

    if user.role != "hr_admin":
        assigned = db.scalar(
            select(Interview.id).where(
                Interview.candidate_id == candidate_id,
                Interview.interviewer_id == user.id,
            )
        )
        if assigned is None:
            raise HTTPException(404, "Candidate not found")

    ext_data = dict(cand.extracted_data or {})
    return {
        "candidate_id": cand.id,
        "name": cand.name,
        "email": cand.email,
        "extraction_method": cand.extraction_method or "standard",
        "extraction_status": cand.extraction_status or "completed",
        "extracted_data": ext_data,
        "resume_text": cand.resume_text,
        "warnings": ext_data.get("warnings", []),
    }


@router.patch("/candidates/{candidate_id}/extraction")
def update_candidate_extraction(
    candidate_id: int,
    body: ExtractionPatchIn,
    db: Session = Depends(get_db),
    _: User = Depends(require_hr),
):
    """Allows HR to review and correct extracted fields before AI screening."""
    cand = db.get(Candidate, candidate_id)
    if not cand:
        raise HTTPException(404, "Candidate not found")

    current_extracted = dict(cand.extracted_data or {})

    if body.name is not None:
        cand.name = body.name.strip()
        current_extracted["name"] = cand.name

    if body.email is not None:
        email = body.email.strip().lower()
        if email and ("@" not in email or email.endswith("@")):
            raise HTTPException(400, "That email address does not look valid")
        cand.email = email
        current_extracted["email"] = email

    if body.resume_text is not None:
        cand.resume_text = body.resume_text.strip()
        current_extracted["raw_text"] = cand.resume_text

    if body.skills is not None:
        current_extracted["skills"] = [str(s).strip() for s in body.skills if str(s).strip()]

    if body.education is not None:
        current_extracted["education"] = body.education

    if body.experience is not None:
        current_extracted["experience"] = body.experience

    current_extracted["manually_corrected"] = True
    cand.extracted_data = current_extracted
    db.commit()

    return {
        "candidate_id": cand.id,
        "name": cand.name,
        "email": cand.email,
        "extraction_method": cand.extraction_method,
        "extraction_status": cand.extraction_status,
        "extracted_data": cand.extracted_data,
        "resume_text": cand.resume_text,
        "warnings": current_extracted.get("warnings", []),
    }


@router.post("/candidates/{candidate_id}/reprocess")
def reprocess_candidate_resume(
    candidate_id: int,
    body: ReprocessIn | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_hr),
):
    cand = db.get(Candidate, candidate_id)
    if not cand:
        raise HTTPException(404, "Candidate not found")

    mode = body.mode if body and body.mode else None
    if mode and mode not in EXTRACTION_MODES:
        raise HTTPException(400, f"Invalid mode. Must be one of: {', '.join(EXTRACTION_MODES)}")

    service = ResumeProcessingService(db)
    # Process text content directly
    data_bytes = cand.resume_text.encode("utf-8")
    extracted = service.process(
        filename=f"{cand.name}.txt",
        data=data_bytes,
        mode=mode,
        llm=get_llm_optional(),
    )

    cand.extraction_method = extracted.method_used
    cand.extraction_status = extracted.status
    cand.extracted_data = extracted.to_dict()
    if extracted.name and extracted.name != "Unnamed candidate":
        cand.name = extracted.name
    if extracted.email:
        cand.email = extracted.email
    if extracted.raw_text:
        cand.resume_text = extracted.raw_text

    db.commit()
    return {
        "candidate_id": cand.id,
        "name": cand.name,
        "email": cand.email,
        "extraction_method": cand.extraction_method,
        "extraction_status": cand.extraction_status,
        "extracted_data": cand.extracted_data,
        "resume_text": cand.resume_text,
        "warnings": (cand.extracted_data or {}).get("warnings", []),
    }
