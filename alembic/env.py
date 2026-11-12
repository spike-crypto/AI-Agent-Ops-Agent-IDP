from alembic import context
from sqlalchemy import create_engine, pool

from app import models  # noqa: F401  (registers every table on Base.metadata)
from app.config import settings
from app.db import Base, normalise_url

config = context.config
target_metadata = Base.metadata


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=normalise_url(settings.database_url), literal_binds=True,
               dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(normalise_url(settings.database_url), poolclass=pool.NullPool)
    with engine.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
