from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings


def normalise_url(url: str) -> str:
    """Render/Heroku hand out postgres:// URLs; SQLAlchemy needs the psycopg driver."""
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _make_engine(raw_url: str):
    url = normalise_url(raw_url)
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)

    kwargs: dict = {"connect_args": {"check_same_thread": False}}
    if parsed.database in (None, "", ":memory:"):
        kwargs["poolclass"] = StaticPool  # one shared in-memory DB (tests)
    else:
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url, **kwargs)


engine = _make_engine(settings.database_url)


if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _):
        # WAL lets the background worker write progress while the API reads; the busy
        # timeout is what turns "database is locked" races into a short wait.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
