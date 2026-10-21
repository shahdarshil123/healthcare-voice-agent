"""The connection to PostgreSQL (started with: docker compose up -d).

The address comes from the CAREVOICE_DATABASE_URL environment variable, so tests and other machines can
point somewhere else. The default matches docker-compose.yml and is for local development only.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DATABASE_URL = "postgresql+psycopg://carevoice:carevoice-local-dev@127.0.0.1:5432/carevoice"


def database_url() -> str:
    return os.environ.get("CAREVOICE_DATABASE_URL", DEFAULT_DATABASE_URL)


def make_engine(url: str | None = None) -> Engine:
    """An engine manages a pool of connections. pool_pre_ping checks a connection still works before using it."""
    return create_engine(url or database_url(), pool_pre_ping=True)


def make_session_factory(engine: Engine) -> "sessionmaker[Session]":  # quoted: Pylint misreads the subscript
    """A Session is one unit of work: everything done in it is committed (or rolled back) together."""
    return sessionmaker(bind=engine, expire_on_commit=False)
