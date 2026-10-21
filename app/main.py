"""The CareVoice AI web server.

Run:
    uv run fastapi dev app/main.py --port 8010
"""

import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from app.availability_lookup import look_up_availability
from app.conversations import get_history, new_conversation_id, save_turn
from app.database import make_engine, make_session_factory
from app.departments import look_up_departments
from app.doctors import look_up_doctors
from app.knowledge import KnowledgeError, search
from app.llm import LLMError, add_documents_to_question, chat
from app.speech import AudioError, transcribe

# Show our diagnostic log lines (e.g. "carevoice.retrieval") in the server's terminal.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # hide httpx's line for every request it sends
logger = logging.getLogger("carevoice.answer")

# The web page lives in the "frontend" folder next to the "app" folder.
FRONTEND_PAGE = Path(__file__).parent.parent / "frontend" / "index.html"

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

# The application object. Every endpoint is attached to it.
app = FastAPI(title="CareVoice AI")


# ---------- Shared steps, used by more than one endpoint ----------

def transcribe_upload(file: UploadFile) -> dict:
    """Check an uploaded audio file, then turn it into text."""
    # Check the input before doing any expensive work.
    if not (file.content_type or "").startswith("audio/"):
        raise HTTPException(
            status_code=415,
            detail="Please upload an audio file (for example WAV, MP3, M4A or WebM).",
        )
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="The file is too large. The limit is 10 MB.")

    try:
        return transcribe(file.file)
    except AudioError as error:
        raise HTTPException(
            status_code=422,
            detail="The audio could not be read. The file may be damaged or empty.",
        ) from error


UNAVAILABLE_MESSAGE = "The assistant is unavailable right now. Please try again in a moment."


def seconds_since(start: float) -> float:
    return round(time.perf_counter() - start, 3)


# The booking database (docker compose up -d). Connections are only opened when a message needs one.
booking_database = make_session_factory(make_engine())


def availability_facts_for(text: str, conversation_id: str) -> list[str]:
    """Real free times for the doctor and day asked about (read-only).

    If the booking database is down, the chat still works, just without availability facts.
    """
    earlier = [m["content"] for m in get_history(conversation_id) if m["role"] == "user"]
    try:
        with booking_database() as session:
            return look_up_availability(session, text, earlier)
    except SQLAlchemyError:
        logger.warning(json.dumps({"event": "availability_unavailable", "conversation_id": conversation_id}))
        return []


def answer(conversation_id: str, text: str) -> dict:
    """Answer the user's message from the hospital documents and lookups, and remember the turn."""
    start = time.perf_counter()

    # 1. Find the document excerpts relevant to this message.
    try:
        sources = search(text)
    except KnowledgeError as error:
        raise HTTPException(status_code=503, detail=UNAVAILABLE_MESSAGE) from error
    search_seconds = seconds_since(start)

    # 2. Check doctor and department names against the hospital's complete lists, and look up real free
    #    times if the message asks about them (all exact and deterministic; read-only).
    lookup_start = time.perf_counter()
    doctor_facts = look_up_doctors(text)
    department_facts = look_up_departments(text)
    availability_facts = availability_facts_for(text, conversation_id)
    lookup_seconds = seconds_since(lookup_start)

    # 3. Send the conversation so far, plus this message with its excerpts and lookups attached, to the model.
    question_with_documents = {
        "role": "user",
        "content": add_documents_to_question(text, sources, doctor_facts, department_facts, availability_facts),
    }
    try:
        result = chat(get_history(conversation_id) + [question_with_documents])
    except LLMError as error:
        raise HTTPException(status_code=503, detail=UNAVAILABLE_MESSAGE) from error

    # 4. Remember the plain message (without the excerpts), and only after it succeeded.
    save_turn(
        conversation_id,
        {"role": "user", "content": text},
        {"role": "assistant", "content": result["reply"]},
    )

    result["sources"] = [{"title": source.title, "section": source.section} for source in sources]
    # Diagnostics: exactly which evidence the answer was based on, and where the time went.
    result["retrieval"] = [
        {"chunk_id": source.chunk_id, "doc_id": source.doc_id, "distance": source.distance} for source in sources
    ]
    result["lookups"] = {"doctors": len(doctor_facts), "departments": len(department_facts),
                         "availability": len(availability_facts)}
    # The verified facts the model was given, for debugging ("why did it say 14:15?") and for the answer
    # evaluation's fact checker. They contain hospital data and free times only, never patient data.
    result["facts"] = doctor_facts + department_facts + availability_facts
    result["timings"] = {
        "search_seconds": search_seconds,
        "lookup_seconds": lookup_seconds,
        "llm_seconds": result["seconds"],
        "total_seconds": seconds_since(start),
    }
    logger.info(json.dumps({
        "event": "answer",
        "conversation_id": conversation_id,
        "model": result["model"],
        "prompt_version": result["prompt_version"],
        "chunk_ids": [source.chunk_id for source in sources],
        "lookups": result["lookups"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        **result["timings"],
    }))
    return result


# ---------- Endpoints ----------

@app.get("/")
def home():
    return FileResponse(FRONTEND_PAGE)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/transcribe")
def transcribe_audio(file: UploadFile):
    """Speech to text only."""
    result = transcribe_upload(file)
    result["filename"] = file.filename
    return result


class ChatRequest(BaseModel):
    """What the browser sends to /chat."""

    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None  # leave empty to start a new conversation


@app.post("/chat")
def chat_with_assistant(request: ChatRequest):
    """A typed message in, the assistant's reply out."""
    conversation_id = request.conversation_id or new_conversation_id()
    result = answer(conversation_id, request.message)
    result["conversation_id"] = conversation_id
    return result


@app.post("/voice-chat")
def voice_chat(file: UploadFile, conversation_id: str | None = Form(default=None)):
    """A recording in, the assistant's reply out: speech-to-text and the LLM in one request."""
    start_time = time.perf_counter()
    conversation_id = conversation_id or new_conversation_id()

    speech = transcribe_upload(file)
    if not speech["text"]:
        raise HTTPException(status_code=422, detail="No speech was detected. Please try again.")

    result = answer(conversation_id, speech["text"])

    return {
        "transcript": speech["text"],
        "reply": result["reply"],
        "sources": result["sources"],
        "retrieval": result["retrieval"],
        "lookups": result["lookups"],
        "facts": result["facts"],
        "conversation_id": conversation_id,
        "model": result["model"],
        "prompt_version": result["prompt_version"],
        "audio_seconds": speech["audio_seconds"],
        "transcribe_seconds": speech["processing_seconds"],
        "llm_seconds": result["seconds"],
        "total_seconds": round(time.perf_counter() - start_time, 2),
    }
