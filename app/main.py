import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from app.config import settings
from app.db import Base, SessionLocal, engine, get_db
from app.deps import require_hr
from app.jobqueue import start_worker, stop_worker
from app.llm import LLM, LLMError, get_llm_optional, llm_status, parse_json
from app.logging_config import get_logger, request_id_var, setup_logging
from app.models import User
from app.observability import init_observability, shutdown_observability
from app.security import hash_password

setup_logging()
log = get_logger("hr_agents.main")

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
    """Create/upgrade the schema, and the first HR admin if there are no users."""
    if settings.app_env == "test":
        Base.metadata.create_all(engine)  # fast, isolated unit tests
    else:
        from app.migrations import run_migrations

        run_migrations()
    if not (settings.admin_email and settings.admin_password):
        return
    with SessionLocal() as db:
        if db.scalar(select(User.id).limit(1)) is None:
            db.add(User(email=settings.admin_email.lower(), name=settings.admin_name,
                        password_hash=hash_password(settings.admin_password), role="hr_admin"))
            db.commit()
            log.info("admin.bootstrapped", extra={"user": settings.admin_email})


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    init_observability()
    if settings.run_worker and settings.app_env != "test":
        start_worker()
    log.info("app.started", extra={"version": VERSION, "env": settings.app_env,
                                   "llm_configured": settings.llm_configured})
    try:
        yield
    finally:
        stop_worker()
        shutdown_observability()
        log.info("app.stopped")


app = FastAPI(title="Team API - HR Agents", lifespan=lifespan)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """A request id on every log line, an access log with duration, and no-cache UI."""
    request_id = (request.headers.get("x-request-id") or uuid.uuid4().hex)[:32]
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("http.unhandled", extra={"method": request.method, "path": request.url.path,
                                                "duration_ms": int((time.perf_counter() - started) * 1000)})
        raise
    duration_ms = int((time.perf_counter() - started) * 1000)
    response.headers["X-Request-ID"] = request_id
    if request.url.path == "/" or request.url.path.startswith(("/static/", "/careers")):
        response.headers["Cache-Control"] = "no-cache"
    quiet = request.url.path.startswith(("/static/", "/health"))
    log.log(logging.DEBUG if quiet else logging.INFO, "http.request",
            extra={"method": request.method, "path": request.url.path,
                   "status": response.status_code, "duration_ms": duration_ms})
    request_id_var.reset(token)
    return response


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    log.error("http.crashed", exc_info=exc,
              extra={"method": request.method, "path": request.url.path})
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": request_id_var.get()},
    )


@app.get("/health")
def health(db=Depends(get_db)):
    """Liveness + readiness: the database answers and the AI provider is configured."""
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:
        database = "error"
    body = {"status": "ok" if database == "ok" else "degraded", "version": VERSION,
            "database": database, "llm": llm_status()}
    return JSONResponse(status_code=200 if database == "ok" else 503, content=body)


@app.get("/health/ai")
def health_ai(_: User = Depends(require_hr), llm: LLM | None = Depends(get_llm_optional)):
    """A real, tiny model call: proves the key, model names and network all work."""
    if llm is None:
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "OPENROUTER_API_KEY is not set", **llm_status()})
    started = time.perf_counter()
    try:
        answer = llm.complete(
            "You are a health check for an HR assistant. Answer with JSON only.",
            'Reply with exactly {"ok": true, "service": "hiring-desk"}',
            json_mode=True,
        )
        parsed = parse_json(answer)
    except LLMError as exc:
        log.warning("ai.health_failed", extra={"error": str(exc)[:500]})
        return JSONResponse(status_code=503, content={
            "ok": False, "error": str(exc), "latency_ms": int((time.perf_counter() - started) * 1000),
            **llm_status()})
    last = dict(getattr(llm, "last_call", {}) or {})
    log.info("ai.health_ok", extra={"model": last.get("model"),
                                    "latency_ms": int((time.perf_counter() - started) * 1000)})
    return {"ok": True, "model": last.get("model"), "reply": parsed,
            "latency_ms": int((time.perf_counter() - started) * 1000), **llm_status()}


@app.get("/", include_in_schema=False)
def ui():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/careers", include_in_schema=False)
def careers():
    return FileResponse(STATIC_DIR / "careers.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --- Feature routers: add yours below this line ---
from app.routers import copilot, agents, auth, dashboard, drafts, interviews, jobs, public  # noqa: E402

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(jobs.router)
app.include_router(interviews.router)
app.include_router(drafts.router)
app.include_router(agents.router)
app.include_router(public.router)
app.include_router(copilot.router)
