"""Automatic checks for the CareVoice AI server.

Run:
    uv run pytest
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import MAX_UPLOAD_BYTES, app

SAMPLE_AUDIO = Path(__file__).parent.parent / "data" / "samples" / "demo-question.wav"

# A pretend browser that sends requests straight to our app, without starting a real server.
client = TestClient(app)


def test_health_says_ok():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_home_page_is_served():
    response = client.get("/")

    assert response.status_code == 200
    assert "Start recording" in response.text


def test_transcribes_real_audio():
    with open(SAMPLE_AUDIO, "rb") as audio:
        response = client.post("/transcribe", files={"file": ("demo.wav", audio, "audio/wav")})

    assert response.status_code == 200
    result = response.json()
    assert "appointment" in result["text"].lower()
    assert result["audio_seconds"] > 0
    assert result["filename"] == "demo.wav"


def test_rejects_a_file_that_is_not_audio():
    response = client.post("/transcribe", files={"file": ("notes.txt", b"hello", "text/plain")})

    assert response.status_code == 415
    assert "audio file" in response.json()["detail"]


def test_rejects_damaged_audio():
    fake_audio = b"this is not really audio" * 10
    response = client.post("/transcribe", files={"file": ("broken.wav", fake_audio, "audio/wav")})

    assert response.status_code == 422
    assert "could not be read" in response.json()["detail"]


def test_rejects_a_file_that_is_too_large():
    too_big = b"\0" * (MAX_UPLOAD_BYTES + 1)
    response = client.post("/transcribe", files={"file": ("huge.wav", too_big, "audio/wav")})

    assert response.status_code == 413


def test_rejects_a_request_with_no_file():
    response = client.post("/transcribe")

    assert response.status_code == 422
