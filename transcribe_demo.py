"""Transcribe one audio file with Whisper and print the text.

Usage:
    uv run python transcribe_demo.py path/to/audio.wav
"""

import sys
import time

from faster_whisper import WhisperModel

# 1. Which file? It's the first thing typed after the script name.
audio_path = sys.argv[1]

# 2. Load the AI model. The first run downloads it (~145 MB); later runs use the saved copy.
print("Loading the Whisper model...")
model = WhisperModel("base.en", device="cpu", compute_type="int8")

# 3. Transcribe. Whisper splits speech into "segments", usually one per sentence or phrase.
print(f"Transcribing {audio_path} ...")
start_time = time.perf_counter()
segments, info = model.transcribe(audio_path)

for segment in segments:
    print(f"[{segment.start:5.1f}s -> {segment.end:5.1f}s] {segment.text}")

# 4. How long did it take compared with the length of the audio?
elapsed = time.perf_counter() - start_time
print(f"Audio length: {info.duration:.1f} s | Transcription took: {elapsed:.1f} s")
