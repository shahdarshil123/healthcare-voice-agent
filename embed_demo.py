"""See what embeddings are: turn sentences into numbers, then compare their meanings.

Usage:
    uv run python embed_demo.py
"""

import math

import httpx

OLLAMA_URL = "http://127.0.0.1:11434/api/embed"
MODEL = "all-minilm"


def embed(texts: list[str]) -> list[list[float]]:
    """Turn each text into a list of numbers (a vector) that represents its meaning."""
    response = httpx.post(OLLAMA_URL, json={"model": MODEL, "input": texts}, timeout=60)
    response.raise_for_status()
    return response.json()["embeddings"]


def similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity: close to 1.0 = similar meaning, close to 0 = unrelated."""
    dot_product = sum(x * y for x, y in zip(a, b))
    length_a = math.sqrt(sum(x * x for x in a))
    length_b = math.sqrt(sum(y * y for y in b))
    return dot_product / (length_a * length_b)


question = "How much will I be charged if I don't show up?"
passages = [
    "Did not attend and did not cancel (no-show): $50",
    "Please cancel or reschedule at least 24 hours before your appointment time.",
    "Visiting hours on general wards are 09:00 to 20:00, every day.",
    "Parents and guardians may visit the pediatrics ward at any time.",
    "The visitor garage is on Lantern Way. The first 30 minutes are free.",
]

# Embed the question and all passages in one request.
vectors = embed([question] + passages)
question_vector = vectors[0]
passage_vectors = vectors[1:]

print(f"Each text became a list of {len(question_vector)} numbers.")
print(f"The question's first 5 numbers: {[round(x, 3) for x in question_vector[:5]]}\n")

print(f'Question: "{question}"')
print("Passages, most similar first:")
scores = [(similarity(question_vector, vector), passage) for vector, passage in zip(passage_vectors, passages)]
for score, passage in sorted(scores, reverse=True):
    print(f"  {score:.2f}  {passage}")
