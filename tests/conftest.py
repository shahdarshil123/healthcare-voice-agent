"""Shared test setup. Pytest loads this file automatically and makes its fixtures available to every test."""

# Pytest passes fixtures in by parameter name, which Pylint misreads as unused or shadowed names.
# pylint: disable=redefined-outer-name,unused-argument

from dataclasses import dataclass, field

import pytest

from app import conversations, main


@pytest.fixture(autouse=True)
def empty_memory(monkeypatch):
    """Give every test an empty conversation memory, so tests can't affect each other."""
    monkeypatch.setattr(conversations, "_conversations", {})


@pytest.fixture
def no_documents(monkeypatch):
    """Replace the real knowledge base search (which needs Ollama) with one that finds nothing."""
    monkeypatch.setattr(main, "search", lambda question: [])


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


@pytest.fixture
def fake_speech(monkeypatch):
    """Replace Whisper with a fake that always 'hears' the same sentence."""

    def fake_transcribe(audio):
        return {"text": "What are the visiting hours?", "audio_seconds": 2.0, "processing_seconds": 0.1}

    monkeypatch.setattr(main, "transcribe", fake_transcribe)
