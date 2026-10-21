"""How Alembic connects to the database and finds our tables when running migrations.

Commands (run from the project folder, with PostgreSQL running):
    uv run alembic upgrade head          apply every migration not yet applied
    uv run alembic current               show which migration the database is at
    uv run alembic upgrade head --sql    print the SQL instead of running it
    uv run alembic revision --autogenerate -m "describe the change"   draft a new migration from app/models.py
"""

from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context
from app.database import database_url
from app.models import Base

config = context.config

# Use alembic.ini's logging settings when run from the command line (not when tests call Alembic).
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name)

# Our tables, so "--autogenerate" can compare them with the database.
target_metadata = Base.metadata

# The database address: one set by the caller (the tests use a separate test database), else the default.
URL = config.get_main_option("sqlalchemy.url") or database_url()


def run_migrations_offline() -> None:
    """Print the SQL instead of running it (alembic upgrade head --sql)."""
    context.configure(url=URL, target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply the migrations. Each migration runs in a transaction: all of it applies, or none."""
    engine = create_engine(URL, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
