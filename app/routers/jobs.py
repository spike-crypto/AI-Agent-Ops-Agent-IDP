import io

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, require_hr
from app.models import CANDIDATE_STATUSES, Candidate, Interview, Job, User

router = APIRouter(tags=["jobs & candidates"])


class JobIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    department: str = ""
    requirements: str = Field(min_length=1)
    salary_band: str = ""


class JobPatch(BaseModel):
    title: str | None = None
    department: str | None = None
    requirements: str | None = None
    description: str | None = None
    salary_band: str | None = None
    status: str | None = None


class CandidateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = ""
    resume_text: str = Field(min_length=1)


class StatusIn(BaseModel):
    status: str


def job_out(j: Job) -> dict:
    return {"id": j.id, "title": j.title, "department": j.department, "requirements": j.requirements,
            "description": j.description, "salary_band": j.salary_band, "status": j.status}


def candidate_out(c: Candidate, full: bool = True) -> dict:
    data = {"id": c.id, "job_id": c.job_id, "name": c.name, "email": c.email,
            "resume_text": c.resume_text, "status": c.status}
    if full:  # HR only: interviewers don't see agent scores, so they judge independently
        data.update(score=c.score, screening=c.screening, feedback_summary=c.feedback_summary)
    return data


def _job_or_404(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@router.get("/jobs")
def list_jobs(db: Session = Depends(get_db), _: User = Depends(require_hr)):
    return [job_out(j) for j in db.scalars(select(Job).order_by(Job.id.desc()))]


@router.post("/jobs", status_code=201)
def create_job(body: JobIn, db: Session = Depends(get_db), user: User = Depends(require_hr)):
    job = Job(**body.model_dump(), created_by=user.id)
    db.add(job)
    db.commit()
    return job_out(job)


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db), _: User = Depends(require_hr)):
    return job_out(_job_or_404(db, job_id))


@router.patch("/jobs/{job_id}")
def update_job(job_id: int, body: JobPatch, db: Session = Depends(get_db), _: User = Depends(require_hr)):
    job = _job_or_404(db, job_id)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(job, field, value)
    db.commit()
    return job_out(job)


@router.get("/jobs/{job_id}/candidates")
def list_candidates(job_id: int, db: Session = Depends(get_db), _: User = Depends(require_hr)):
    _job_or_404(db, job_id)
    rows = db.scalars(select(Candidate).where(Candidate.job_id == job_id))
    # Highest score first, unscreened at the bottom.
    return sorted((candidate_out(c) for c in rows), key=lambda c: (c["score"] is None, -(c["score"] or 0)))


@router.post("/jobs/{job_id}/candidates", status_code=201)
def add_candidate(job_id: int, body: CandidateIn, db: Session = Depends(get_db), _: User = Depends(require_hr)):
    _job_or_404(db, job_id)
    cand = Candidate(job_id=job_id, **body.model_dump())
    db.add(cand)
    db.commit()
    return candidate_out(cand)


MAX_UPLOAD_BYTES = 2_000_000


def _extract_text(filename: str, data: bytes) -> str:
    if filename.lower().endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    return data.decode("utf-8", errors="replace")


@router.post("/jobs/{job_id}/candidates/upload", status_code=201)
async def upload_resumes(job_id: int, files: list[UploadFile], db: Session = Depends(get_db),
                         _: User = Depends(require_hr)):
    """Upload .txt or .pdf resumes. The file name becomes the candidate name (edit later)."""
    _job_or_404(db, job_id)
    created, skipped = [], []
    for f in files:
        name = f.filename or "resume"
        if not name.lower().endswith((".txt", ".pdf", ".md")):
            skipped.append({"file": name, "reason": "only .txt, .md and .pdf are supported"})
            continue
        data = await f.read()
        if len(data) > MAX_UPLOAD_BYTES:
            skipped.append({"file": name, "reason": "file larger than 2 MB"})
            continue
        try:
            text = _extract_text(name, data).strip()
        except Exception as exc:
            skipped.append({"file": name, "reason": f"could not read file: {exc}"})
            continue
        if not text:
            skipped.append({"file": name, "reason": "no text found (scanned PDF?)"})
            continue
        email = next((w.strip(".,;()<>") for w in text.split() if "@" in w and "." in w), "")
        display = name.rsplit(".", 1)[0].replace("_", " ").replace("-", " ").title()
        cand = Candidate(job_id=job_id, name=display, email=email, resume_text=text)
        db.add(cand)
        created.append(cand)
    db.commit()
    return {"created": [candidate_out(c) for c in created], "skipped": skipped}


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    cand = db.get(Candidate, candidate_id)
    if cand is None:
        raise HTTPException(404, "Candidate not found")
    if user.role == "hr_admin":
        return candidate_out(cand)
    # Interviewers only see candidates they are interviewing.
    assigned = db.scalar(select(Interview.id).where(
        Interview.candidate_id == candidate_id, Interview.interviewer_id == user.id))
    if assigned is None:
        raise HTTPException(404, "Candidate not found")  # 404, not 403: don't reveal it exists
    return candidate_out(cand, full=False)


@router.post("/candidates/{candidate_id}/status")
def set_status(candidate_id: int, body: StatusIn, db: Session = Depends(get_db), _: User = Depends(require_hr)):
    """The human decision: shortlist, reject, offer. Agents never change this to a decision."""
    if body.status not in CANDIDATE_STATUSES:
        raise HTTPException(400, f"Status must be one of {CANDIDATE_STATUSES}")
    cand = db.get(Candidate, candidate_id)
    if cand is None:
        raise HTTPException(404, "Candidate not found")
    cand.status = body.status
    db.commit()
    return candidate_out(cand)
