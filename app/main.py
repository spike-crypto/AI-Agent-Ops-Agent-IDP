import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models import User
from app.observability import init_observability
from app.security import hash_password

logging.basicConfig(level=logging.INFO)

VERSION_FILE = Path(__file__).parent / "version.txt"

# The smoke test compares this with the merge commit. version.txt wins if a pipeline wrote one;
# on Render, RENDER_GIT_COMMIT is the commit Render built.
def _version() -> str:
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text().strip()
    return os.getenv("RENDER_GIT_COMMIT") or "dev"


VERSION = _version()
STATIC_DIR = Path(__file__).parent / "static"


def init_db() -> None:
    """Create tables, and the first HR admin from ADMIN_EMAIL / ADMIN_PASSWORD if there are no users."""
    Base.metadata.create_all(engine)
    if not (settings.admin_email and settings.admin_password):
        return
    with SessionLocal() as db:
        if db.scalar(select(User.id).limit(1)) is None:
            db.add(User(email=settings.admin_email.lower(), name=settings.admin_name,
                        password_hash=hash_password(settings.admin_password), role="hr_admin"))
            db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    init_observability()
    yield


app = FastAPI(title="Team API - HR Agents", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "version": VERSION}


@app.get("/", include_in_schema=False)
def ui():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --- Feature routers: add yours below this line ---
from app.routers import agents, auth, dashboard, drafts, interviews, jobs  # noqa: E402

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(jobs.router)
app.include_router(interviews.router)
app.include_router(drafts.router)
app.include_router(agents.router)
