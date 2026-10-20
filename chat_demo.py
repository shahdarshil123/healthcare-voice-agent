"""Send one question to the local LLM (through Ollama) and print the answer.

Usage:
    uv run python chat_demo.py "What should I bring to a doctor's appointment?"
"""

import sys

import httpx

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen3:1.7b"

# 1. The question is the first thing typed after the script name.
question = sys.argv[1]

# 2. Build the request. "messages" is the conversation so far; here it's just one question.
request_body = {
    "model": MODEL,
    "messages": [
        {"role": "user", "content": question},
    ],
    "stream": False,  # wait for the whole answer instead of receiving it word by word
    "think": False,   # skip Qwen3's "thinking" step, for faster answers
}

# 3. Send it to Ollama and wait (up to 60 seconds) for the reply.
print(f"Asking {MODEL}...")
response = httpx.post(OLLAMA_URL, json=request_body, timeout=60)
response.raise_for_status()  # stop with an error if Ollama replied with an error code
result = response.json()

# 4. The answer is inside result["message"]["content"].
print("\nAnswer:")
print(result["message"]["content"])

# 5. Ollama also reports how much work it did. Durations are in nanoseconds.
tokens = result["eval_count"]
seconds = result["eval_duration"] / 1_000_000_000
total_seconds = result["total_duration"] / 1_000_000_000
print(f"\n{tokens} tokens in {seconds:.1f} s ({tokens / seconds:.0f} tokens/s), total {total_seconds:.1f} s")
