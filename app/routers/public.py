"""Free public careers page: open jobs and a one-page application.

No account, no fee, no candidate data exposed back. Applications land in the same
pipeline as uploaded resumes, tagged source='careers'.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import EmailStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ratelimit
from app.config import settings
from app.db import get_db
from app.logging_config import get_logger
from app.models import Candidate, Job
from app.resume_parsing import extract_text, fingerprint

router = APIRouter(prefix="/public", tags=["public careers"])
log = get_logger("hr_agents.public")

MAX_UPLOAD_BYTES = 2_000_000
_ALLOWED = (".txt", ".md", ".pdf")


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "-")


def _public_job(job: Job) -> dict:
    return {
        "id": job.id,
        "title": job.title,
        "department": job.department,
        "location": job.location,
        "description": job.description or job.requirements,
        "requirements": job.requirements,
    }


@router.get("/config")
def public_config():
    return {"company_name": settings.company_name, "apply_rate_limit_per_hour": settings.public_apply_per_hour}


@router.get("/jobs")
def list_public_jobs(db: Session = Depends(get_db)):
    jobs = db.scalars(
        select(Job).where(Job.published.is_(True), Job.status == "open").order_by(Job.id.desc())
    )
    return [_public_job(j) for j in jobs]


@router.get("/jobs/{job_id}")
def public_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None or not job.published or job.status != "open":
        raise HTTPException(404, "That job is not open")
    return _public_job(job)


@router.post("/jobs/{job_id}/apply", status_code=201)
async def apply(job_id: int, request: Request, name: str = Form(..., min_length=1, max_length=200),
                email: EmailStr = Form(...), resume: UploadFile = File(...),
                note: str = Form("", max_length=2000), db: Session = Depends(get_db)):
    allowed, retry = ratelimit.hit(f"apply:ip:{_client_ip(request)}",
                                   settings.public_apply_per_hour, 3600)
    if not allowed:
        raise HTTPException(429, "Too many applications from this connection. Try again later.",
                            headers={"Retry-After": str(retry)})

    job = db.get(Job, job_id)
    if job is None or not job.published or job.status != "open":
        raise HTTPException(404, "That job is not open")

    filename = (resume.filename or "resume").lower()
    if not filename.endswith(_ALLOWED):
        raise HTTPException(400, "Please attach your resume as a .pdf, .txt or .md file")
    data = await resume.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Your resume is larger than 2 MB")
    try:
        text = extract_text(filename, data)
    except Exception:
        raise HTTPException(400, "We could not read that file. Try another PDF or a .txt file.")
    if not text:
        raise HTTPException(400, "No text was found in that file. A scanned PDF cannot be read.")

    address = str(email).lower().strip()
    existing = list(db.scalars(select(Candidate).where(Candidate.job_id == job_id)))
    if any(c.email == address for c in existing if c.email) or \
            any(fingerprint(c.resume_text) == fingerprint(text) for c in existing):
        raise HTTPException(409, "You have already applied for this job")

    resume_text = text + (f"\n\nAPPLICANT NOTE:\n{note.strip()}" if note.strip() else "")
    cand = Candidate(job_id=job_id, name=name.strip(), email=address,
                     resume_text=resume_text, source="careers")
    db.add(cand)
    db.commit()
    log.info("public.application", extra={"job_id": job_id, "candidate_id": cand.id})
    return {"id": cand.id, "status": "received",
            "message": "Thanks. Your application is in review; the team will contact you by email."}
