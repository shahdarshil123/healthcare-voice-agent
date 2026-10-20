"""Tests for /chat and /voice-chat, using a FAKE language model.

The real model is slow, needs Ollama running, and words its answers differently every time.
These tests check OUR code (memory, the pipeline, error handling), not the quality of the
model's answers, so a fake model that replies instantly and predictably is all we need.

Run:
    uv run pytest
"""

# Pytest passes fixtures in by parameter name, which Pylint misreads as unused or shadowed names.
# pylint: disable=redefined-outer-name,unused-argument

import httpx
import pytest
from fastapi.testclient import TestClient

from app import conversations, llm, main
from app.llm import LLMError, add_documents_to_question

client = TestClient(main.app)

FAKE_REPLY = "This is a fake reply."


# ---------- Fixtures: setup that tests can ask for by name ----------

@pytest.fixture(autouse=True)
def empty_memory(monkeypatch):
    """Give every test an empty conversation memory, so tests can't affect each other."""
    monkeypatch.setattr(conversations, "_conversations", {})


@pytest.fixture(autouse=True)
def no_documents(monkeypatch):
    """Replace the real knowledge base search (which needs Ollama) with one that finds nothing."""
    monkeypatch.setattr(main, "search", lambda question: [])


def as_sent(text):
    """How the current message looks when sent to the model: with its (empty) documents block attached."""
    return {"role": "user", "content": add_documents_to_question(text, [])}


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace the real model with a fake one. Returns the list of conversations it was sent."""
    received = []

    def fake_chat(messages):
        received.append(messages)
        return {
            "reply": FAKE_REPLY,
            "model": "fake-model",
            "prompt_version": "test",
            "input_tokens": 0,
            "output_tokens": 0,
            "seconds": 0.0,
        }

    # Replace "chat" inside app.main, because that's where the endpoints look it up.
    monkeypatch.setattr(main, "chat", fake_chat)
    return received


@pytest.fixture
def fake_speech(monkeypatch):
    """Replace Whisper with a fake that always 'hears' the same sentence."""

    def fake_transcribe(audio):
        return {"text": "What are the visiting hours?", "audio_seconds": 2.0, "processing_seconds": 0.1}

    monkeypatch.setattr(main, "transcribe", fake_transcribe)


def upload_recording(conversation_id=None):
    """Send a pretend recording to /voice-chat, the way the browser does."""
    form_fields = {"conversation_id": conversation_id} if conversation_id else {}
    return client.post(
        "/voice-chat",
        files={"file": ("recording.webm", b"pretend audio", "audio/webm")},
        data=form_fields,
    )


# ---------- /chat ----------

def test_chat_returns_the_reply_and_a_new_conversation_id(fake_llm):
    response = client.post("/chat", json={"message": "Hello"})

    assert response.status_code == 200
    result = response.json()
    assert result["reply"] == FAKE_REPLY
    assert len(result["conversation_id"]) == 32


def test_chat_sends_earlier_messages_to_the_model(fake_llm):
    first = client.post("/chat", json={"message": "My name is Darshil"}).json()
    client.post("/chat", json={"message": "What is my name?", "conversation_id": first["conversation_id"]})

    # The second time, the model must receive the whole conversation, in order.
    # Earlier messages are stored plain; only the current one has documents attached.
    assert fake_llm[1] == [
        {"role": "user", "content": "My name is Darshil"},
        {"role": "assistant", "content": FAKE_REPLY},
        as_sent("What is my name?"),
    ]


def test_separate_conversations_do_not_share_memory(fake_llm):
    client.post("/chat", json={"message": "My name is Darshil"})
    client.post("/chat", json={"message": "What is my name?"})  # no ID: a new conversation

    assert fake_llm[1] == [as_sent("What is my name?")]


def test_chat_rejects_an_empty_message(fake_llm):
    response = client.post("/chat", json={"message": ""})

    assert response.status_code == 422
    assert fake_llm == []  # the model was never called


def test_chat_returns_503_and_remembers_nothing_when_the_model_is_down(monkeypatch):
    def broken_chat(messages):
        raise LLMError("Ollama is not running.")

    monkeypatch.setattr(main, "chat", broken_chat)
    response = client.post("/chat", json={"message": "Hello", "conversation_id": "abc"})

    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]
    assert conversations.get_history("abc") == []  # the failed turn was not saved


# ---------- /voice-chat ----------

def test_voice_chat_transcribes_and_answers_in_one_request(fake_speech, fake_llm):
    response = upload_recording()

    assert response.status_code == 200
    result = response.json()
    assert result["transcript"] == "What are the visiting hours?"
    assert result["reply"] == FAKE_REPLY
    # The model was given exactly what Whisper heard.
    assert fake_llm[0] == [as_sent("What are the visiting hours?")]


def test_voice_chat_continues_an_existing_conversation(fake_speech, fake_llm):
    first = client.post("/chat", json={"message": "My name is Darshil"}).json()
    upload_recording(conversation_id=first["conversation_id"])

    assert len(fake_llm[1]) == 3  # typed message + its reply + the spoken message


def test_voice_chat_does_not_call_the_model_when_nothing_was_heard(monkeypatch, fake_llm):
    monkeypatch.setattr(main, "transcribe", lambda audio: {"text": "", "audio_seconds": 2.0, "processing_seconds": 0.1})
    response = upload_recording()

    assert response.status_code == 422
    assert "No speech" in response.json()["detail"]
    assert fake_llm == []


def test_voice_chat_rejects_a_file_that_is_not_audio(fake_llm):
    response = client.post("/voice-chat", files={"file": ("notes.txt", b"hello", "text/plain")})

    assert response.status_code == 415
    assert fake_llm == []


# ---------- app/llm.py, without Ollama ----------

def test_llm_puts_the_system_prompt_first(monkeypatch):
    sent = {}

    def fake_post(url, json, timeout):
        sent["messages"] = json["messages"]
        return httpx.Response(200, json={"message": {"content": "Hi!"}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    result = llm.chat([{"role": "user", "content": "Hello"}])

    assert result["reply"] == "Hi!"
    assert result["prompt_version"] == llm.PROMPT_VERSION
    assert sent["messages"][0] == {"role": "system", "content": llm.SYSTEM_PROMPT}
    assert sent["messages"][1] == {"role": "user", "content": "Hello"}


def test_llm_reports_a_clear_error_when_ollama_is_not_running(monkeypatch):
    def refuse_connection(url, json, timeout):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(llm.httpx, "post", refuse_connection)

    with pytest.raises(LLMError, match="Ollama is not running"):
        llm.chat([{"role": "user", "content": "Hello"}])


# ---------- app/conversations.py ----------

def test_memory_keeps_only_the_most_recent_messages():
    for turn in range(15):  # 15 turns = 30 messages, more than MAX_MESSAGES
        conversations.save_turn(
            "abc",
            {"role": "user", "content": f"question {turn}"},
            {"role": "assistant", "content": f"answer {turn}"},
        )

    history = conversations.get_history("abc")
    assert len(history) == conversations.MAX_MESSAGES
    assert history[-1] == {"role": "assistant", "content": "answer 14"}  # the newest is kept
    assert history[0] == {"role": "user", "content": "question 5"}       # the oldest were dropped
