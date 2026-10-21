"""Tests for /chat and /voice-chat, using a FAKE language model and a fake search (see conftest.py).

The real model is slow, needs Ollama running, and words its answers differently every time.
These tests check OUR code (memory, the pipeline, lookups, error handling), not the quality of the
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
from app.departments import look_up_departments
from app.doctors import look_up_doctors
from app.knowledge import KnowledgeError, SearchResult
from app.llm import LLMError, add_documents_to_question

# Every test in this file uses the fake search and availability, so none needs Ollama, the knowledge base,
# or the booking database.
pytestmark = pytest.mark.usefixtures("no_documents", "no_availability")

client = TestClient(main.app)

# Kept before any test replaces it with a fake, for the test that needs the real one.
real_availability_facts_for = main.availability_facts_for


def as_sent(text):
    """How the current message looks when sent to the model: with its documents block and lookups attached."""
    return {
        "role": "user",
        "content": add_documents_to_question(text, [], look_up_doctors(text), look_up_departments(text)),
    }


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
    assert result["reply"] == fake_llm.reply
    assert len(result["conversation_id"]) == 32


def test_chat_sends_earlier_messages_to_the_model(fake_llm):
    first = client.post("/chat", json={"message": "My name is Darshil"}).json()
    client.post("/chat", json={"message": "What is my name?", "conversation_id": first["conversation_id"]})

    # The second time, the model must receive the whole conversation, in order.
    # Earlier messages are stored plain; only the current one has documents attached.
    assert fake_llm.received[1] == [
        {"role": "user", "content": "My name is Darshil"},
        {"role": "assistant", "content": fake_llm.reply},
        as_sent("What is my name?"),
    ]


def test_separate_conversations_do_not_share_memory(fake_llm):
    client.post("/chat", json={"message": "My name is Darshil"})
    client.post("/chat", json={"message": "What is my name?"})  # no ID: a new conversation

    assert fake_llm.received[1] == [as_sent("What is my name?")]


def test_chat_rejects_an_empty_message(fake_llm):
    response = client.post("/chat", json={"message": ""})

    assert response.status_code == 422
    assert fake_llm.received == []  # the model was never called


def test_chat_returns_503_and_remembers_nothing_when_the_model_is_down(monkeypatch):
    def broken_chat(messages):
        raise LLMError("Ollama is not running.")

    monkeypatch.setattr(main, "chat", broken_chat)
    response = client.post("/chat", json={"message": "Hello", "conversation_id": "abc"})

    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"]
    assert conversations.get_history("abc") == []  # the failed turn was not saved


def test_chat_returns_503_when_the_knowledge_base_cannot_be_searched(monkeypatch, fake_llm):
    def broken_search(question):
        raise KnowledgeError("The knowledge base could not be searched.")

    monkeypatch.setattr(main, "search", broken_search)
    response = client.post("/chat", json={"message": "What are the visiting hours?"})

    assert response.status_code == 503
    assert fake_llm.received == []


def test_chat_attaches_doctor_and_department_lookups(fake_llm):
    client.post("/chat", json={"message": "Is Dr. Kim in the heart department?"})

    sent = fake_llm.received[0][-1]["content"]
    assert "<doctor_lookup>" in sent and "there is no doctor with this name" in sent
    assert "<department_lookup>" in sent and "Cardiology" in sent


def test_chat_returns_retrieval_diagnostics(monkeypatch, fake_llm):
    found = SearchResult("cancellation-policy#4", "cancellation-policy", "Cancellation Policy", "Fees", "text", 0.31)
    monkeypatch.setattr(main, "search", lambda question: [found])

    result = client.post("/chat", json={"message": "What is the late cancellation fee?"}).json()

    assert result["retrieval"] == [
        {"chunk_id": "cancellation-policy#4", "doc_id": "cancellation-policy", "distance": 0.31},
    ]
    assert result["sources"] == [{"title": "Cancellation Policy", "section": "Fees"}]
    assert set(result["timings"]) == {"search_seconds", "lookup_seconds", "llm_seconds", "total_seconds"}
    assert result["lookups"] == {"doctors": 0, "departments": 0, "availability": 0}


def test_chat_attaches_availability_facts(monkeypatch, fake_llm):
    monkeypatch.setattr(main, "availability_facts_for", lambda text, conversation_id: ["08:00 IS free."])
    client.post("/chat", json={"message": "Is Dr. Patel free on Monday at 8?"})

    sent = fake_llm.received[0][-1]["content"]
    assert "<availability_lookup>\n08:00 IS free.\n</availability_lookup>" in sent


def test_chat_still_works_when_the_booking_database_is_down(monkeypatch, fake_llm):
    def database_down(*args, **kwargs):
        raise main.SQLAlchemyError("connection refused")

    # The real availability lookup this time, with a broken database behind it.
    monkeypatch.setattr(main, "availability_facts_for", real_availability_facts_for)
    monkeypatch.setattr(main, "booking_database", database_down)
    response = client.post("/chat", json={"message": "Is Dr. Patel free on Monday at 8?"})

    assert response.status_code == 200
    assert "<availability_lookup>" not in fake_llm.received[0][-1]["content"]


# ---------- /voice-chat ----------

def test_voice_chat_transcribes_and_answers_in_one_request(fake_speech, fake_llm):
    response = upload_recording()

    assert response.status_code == 200
    result = response.json()
    assert result["transcript"] == "What are the visiting hours?"
    assert result["reply"] == fake_llm.reply
    assert "retrieval" in result
    # The model was given exactly what Whisper heard.
    assert fake_llm.received[0] == [as_sent("What are the visiting hours?")]


def test_voice_chat_continues_an_existing_conversation(fake_speech, fake_llm):
    first = client.post("/chat", json={"message": "My name is Darshil"}).json()
    upload_recording(conversation_id=first["conversation_id"])

    assert len(fake_llm.received[1]) == 3  # typed message + its reply + the spoken message


def test_voice_chat_does_not_call_the_model_when_nothing_was_heard(monkeypatch, fake_llm):
    monkeypatch.setattr(main, "transcribe", lambda audio: {"text": "", "audio_seconds": 2.0, "processing_seconds": 0.1})
    response = upload_recording()

    assert response.status_code == 422
    assert "No speech" in response.json()["detail"]
    assert fake_llm.received == []


def test_voice_chat_rejects_a_file_that_is_not_audio(fake_llm):
    response = client.post("/voice-chat", files={"file": ("notes.txt", b"hello", "text/plain")})

    assert response.status_code == 415
    assert fake_llm.received == []


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


def test_prompt_separates_personal_advice_from_provider_questions():
    # The safety refactor: provider/service questions must be answered, not refused as medical advice.
    assert "are NOT medical advice" in llm.SYSTEM_PROMPT
    assert "MOST IMPORTANT RULE" in llm.SYSTEM_PROMPT and "911" in llm.SYSTEM_PROMPT
    # The old closed-world department list (which made the model deny a pharmacy exists) is gone.
    assert "COMPLETE list of departments" not in llm.SYSTEM_PROMPT


def test_lookup_blocks_are_only_added_when_there_is_something_to_say():
    plain = add_documents_to_question("Hello", [])
    assert "<doctor_lookup>" not in plain and "<department_lookup>" not in plain

    full = add_documents_to_question("Hi", [], ["doctor fact"], ["department fact"])
    assert full.index("<doctor_lookup>") < full.index("<department_lookup>") < full.index("Question: Hi")


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
