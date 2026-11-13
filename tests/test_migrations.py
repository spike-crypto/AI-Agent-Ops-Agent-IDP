"""The migration runner must handle three real starting points:
a brand-new database, a database from before Alembic existed, and a current
database whose alembic_version row was lost. All three must end at head."""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app import migrations
from app.config import settings


def _cfg() -> Config:
    cfg = Config(str(migrations.BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(migrations.BASE_DIR / "alembic"))
    return cfg


def _point_at(monkeypatch, tmp_path):
    url = f"sqlite:///{(tmp_path / 'migrate.db').as_posix()}"
    eng = create_engine(url)
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(migrations, "engine", eng)
    return eng


def _tables(eng) -> set[str]:
    return set(inspect(eng).get_table_names())


def _columns(eng, table: str) -> set[str]:
    return {c["name"] for c in inspect(eng).get_columns(table)}


def _version(eng) -> str | None:
    with eng.connect() as conn:
        return conn.execute(text("select version_num from alembic_version")).scalar()


def test_a_fresh_database_gets_the_full_schema(tmp_path, monkeypatch):
    eng = _point_at(monkeypatch, tmp_path)
    migrations.run_migrations()

    assert _version(eng) == "0004"
    assert {"users", "jobs", "candidates", "interviews", "drafts", "feedback", "agent_runs",
            "refresh_tokens", "password_resets", "agent_jobs"} <= _tables(eng)
    assert {"token_version", "failed_logins", "locked_until", "last_login_at", "mfa_secret",
            "mfa_enabled", "mfa_recovery", "oidc_subject"} <= _columns(eng, "users")
    assert {"location", "published"} <= _columns(eng, "jobs")
    assert "source" in _columns(eng, "candidates")

    migrations.run_migrations()  # running twice is a no-op, not an error
    assert _version(eng) == "0004"


def test_a_pre_alembic_database_is_upgraded_with_its_data_intact(tmp_path, monkeypatch):
    eng = _point_at(monkeypatch, tmp_path)
    command.upgrade(_cfg(), "0001")                        # the old schema...
    with eng.begin() as conn:
        conn.execute(text("drop table alembic_version"))   # ...from before Alembic existed
        conn.execute(text(
            "insert into users (email, name, password_hash, role, is_active, created_at) "
            "values ('old@example.com', 'Old Admin', 'x', 'hr_admin', 1, '2026-01-01 00:00:00')"))
        conn.execute(text(
            "insert into jobs (title, department, requirements, description, salary_band, status, "
            "created_by, created_at) values ('Old Job', 'Eng', 'Python', 'd', 'INR 1', 'open', 1, "
            "'2026-01-01 00:00:00')"))
    assert "refresh_tokens" not in _tables(eng)

    migrations.run_migrations()

    assert _version(eng) == "0004"
    assert {"token_version", "mfa_enabled"} <= _columns(eng, "users")
    assert {"location", "published"} <= _columns(eng, "jobs")
    with eng.connect() as conn:
        assert conn.execute(text("select count(*) from users")).scalar() == 1
        assert conn.execute(text("select token_version, mfa_enabled from users")).one() == (0, 0)
        title, published = conn.execute(text("select title, published from jobs")).one()
        assert title == "Old Job" and not published         # defaults, not NULLs


def test_a_current_schema_missing_its_version_row_is_stamped(tmp_path, monkeypatch):
    eng = _point_at(monkeypatch, tmp_path)
    command.upgrade(_cfg(), "head")
    with eng.begin() as conn:
        conn.execute(text("drop table alembic_version"))

    migrations.run_migrations()

    assert _version(eng) == "0004"
    assert "agent_jobs" in _tables(eng)


def test_a_0002_schema_missing_its_version_row_still_gets_0003(tmp_path, monkeypatch):
    """Stamping straight to head would skip 0003 and leave drafts without trace_id."""
    eng = _point_at(monkeypatch, tmp_path)
    command.upgrade(_cfg(), "0002")
    assert "trace_id" not in _columns(eng, "drafts")
    with eng.begin() as conn:
        conn.execute(text("drop table alembic_version"))

    migrations.run_migrations()

    assert _version(eng) == "0004"
    assert "trace_id" in _columns(eng, "drafts")


def test_a_0003_schema_missing_its_version_row_still_gets_0004(tmp_path, monkeypatch):
    eng = _point_at(monkeypatch, tmp_path)
    command.upgrade(_cfg(), "0003")
    with eng.begin() as conn:
        conn.execute(text("drop table alembic_version"))

    migrations.run_migrations()

    assert _version(eng) == "0004"
    assert {"token_grants", "conversations", "messages", "action_proposals"} <= _tables(eng)
    assert "steps" in _columns(eng, "agent_jobs") and "delegation" in _columns(eng, "agent_runs")


def test_0004_downgrades_cleanly(tmp_path, monkeypatch):
    eng = _point_at(monkeypatch, tmp_path)
    command.upgrade(_cfg(), "head")
    command.downgrade(_cfg(), "0003")
    assert "token_grants" not in _tables(eng) and "steps" not in _columns(eng, "agent_jobs")
