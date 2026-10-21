"""Shared test setup. Pytest loads this file automatically and makes its fixtures available to every test."""

# Pytest passes fixtures in by parameter name, which Pylint misreads as unused or shadowed names.
# pylint: disable=redefined-outer-name,unused-argument

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from alembic import command
from alembic.config import Config
from app import conversations, main
from app.database import database_url, make_engine, make_session_factory

ROOT = Path(__file__).parent.parent
TEST_DATABASE = "carevoice_test"


@pytest.fixture(autouse=True)
def empty_memory(monkeypatch):
    """Give every test an empty conversation memory, so tests can't affect each other."""
    monkeypatch.setattr(conversations, "_conversations", {})


@pytest.fixture
def no_documents(monkeypatch):
    """Replace the real knowledge base search (which needs Ollama) with one that finds nothing."""
    monkeypatch.setattr(main, "search", lambda question: [])


@pytest.fixture
def no_availability(monkeypatch):
    """Replace the availability lookup (which reads the booking database) with one that finds nothing."""
    monkeypatch.setattr(main, "availability_facts_for", lambda text, conversation_id: [])


@dataclass
class FakeLLM:
    """Stands in for app.llm.chat: replies instantly and predictably, and records what it was sent."""

    reply: str = "This is a fake reply."
    received: list = field(default_factory=list)

    def __call__(self, messages):
        self.received.append(messages)
        return {
            "reply": self.reply,
            "model": "fake-model",
            "prompt_version": "test",
            "input_tokens": 0,
            "output_tokens": 0,
            "seconds": 0.0,
        }


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace the real model with a fake one. Use .received to see what was sent, .reply for its answer."""
    fake = FakeLLM()
    # Replace "chat" inside app.main, because that's where the endpoints look it up.
    monkeypatch.setattr(main, "chat", fake)
    return fake


@pytest.fixture(scope="session")
def db_engine():
    """A fresh test database for this test run: created, migrated with Alembic, and seeded. Never the dev database."""
    admin = make_engine().execution_options(isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {TEST_DATABASE} WITH (FORCE)"))
            connection.execute(text(f"CREATE DATABASE {TEST_DATABASE}"))
    except OperationalError:
        pytest.skip("PostgreSQL isn't running. Start it with: docker compose up -d")
    finally:
        admin.dispose()

    url = make_url(database_url()).set(database=TEST_DATABASE).render_as_string(hide_password=False)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logging"] = False
    command.upgrade(config, "head")  # the real migrations, so they're tested too

    engine = make_engine(url)
    from seed import seed_database  # pylint: disable=import-outside-toplevel
    with make_session_factory(engine).begin() as session:
        seed_database(session)
    yield engine
    engine.dispose()


@pytest.fixture
def db(db_engine):
    """A session whose changes are all undone after the test, so every test starts from the seed data."""
    connection = db_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def fake_speech(monkeypatch):
    """Replace Whisper with a fake that always 'hears' the same sentence."""

    def fake_transcribe(audio):
        return {"text": "What are the visiting hours?", "audio_seconds": 2.0, "processing_seconds": 0.1}

    monkeypatch.setattr(main, "transcribe", fake_transcribe)
