"""Alembic is the only thing that changes the schema in production."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.db import engine
from app.logging_config import get_logger

log = get_logger("hr_agents.migrations")
BASE_DIR = Path(__file__).resolve().parent.parent


def run_migrations() -> None:
    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))
    tables = set(inspect(engine).get_table_names())
    if "alembic_version" not in tables:
        if "users" not in tables:
            pass  # fresh database: run 0001 then 0002
        elif "refresh_tokens" in tables:
            # The version row was lost. Work out where the schema really is, so a newer
            # migration is not skipped by stamping straight to head.
            draft_columns = {c["name"] for c in inspect(engine).get_columns("drafts")}
            if "token_grants" in tables:
                at = "head"
            elif "trace_id" in draft_columns:
                at = "0003"
            else:
                at = "0002"
            log.info("migrations.stamp", extra={"revision": at})
            command.stamp(cfg, at)
        else:
            # A database from before Alembic (older local data/hr.db or a Render disk):
            # it matches 0001 exactly, so mark that as applied and upgrade it in place.
            log.info("migrations.stamp_baseline")
            command.stamp(cfg, "0001")
    log.info("migrations.upgrade")
    command.upgrade(cfg, "head")
