"""Speech-to-text: turn recorded audio into text with Whisper."""

import time
from typing import BinaryIO

from av.error import FFmpegError
from faster_whisper import WhisperModel

from app.clinic import CLINIC

MODEL_NAME = "base.en"


def build_vocabulary_hint() -> str:
    """Words Whisper should expect: hospital terms plus every department and doctor in clinic.json.

    Whisper reads this as if it were said just before the recording, which makes these
    words (for example "appointment" or "Dr. Okafor") more likely to be recognised.
    """
    departments = ", ".join(department["name"] for department in CLINIC["departments"])
    doctors = ", ".join(doctor["name"] for doctor in CLINIC["doctors"])
    return (
        f"{CLINIC['hospital']['name']}. Booking, cancelling, or rescheduling an appointment. "
        f"Departments: {departments}. Doctors: {doctors}."
    )


VOCABULARY_HINT = build_vocabulary_hint()


class AudioError(Exception):
    """Raised when the audio can't be read (for example, a damaged or empty file)."""


# The loaded model is kept here so it's only loaded once.
_model = None


def get_model() -> WhisperModel:
    """Load the Whisper model the first time it's needed, then reuse it."""
    global _model
    if _model is None:
        _model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
    return _model


def transcribe(audio: str | BinaryIO) -> dict:
    """Transcribe audio, given as a file path or an open file, and return the text."""
    model = get_model()  # loaded before the timer starts, so loading isn't counted
    start_time = time.perf_counter()

    try:
        # vad_filter=True skips silent parts, so Whisper doesn't "hear" words in silence.
        # initial_prompt gives Whisper our hospital's vocabulary (see build_vocabulary_hint).
        segments, info = model.transcribe(audio, vad_filter=True, initial_prompt=VOCABULARY_HINT)
        text = " ".join(segment.text.strip() for segment in segments)
    except FFmpegError as error:
        # The "av" library couldn't decode the file. Report it as our own, clearer error.
        raise AudioError("The audio could not be read.") from error

    return {
        "text": text,
        "audio_seconds": round(info.duration, 2),
        "processing_seconds": round(time.perf_counter() - start_time, 2),
    }
