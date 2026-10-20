"""The knowledge base: hospital document chunks stored in ChromaDB, searchable by meaning."""

from dataclasses import dataclass
from pathlib import Path

import chromadb
import httpx

from app.documents import Chunk

OLLAMA_EMBED_URL = "http://127.0.0.1:11434/api/embed"
EMBEDDING_MODEL = "all-minilm"

# ChromaDB saves everything in this folder, so the knowledge base survives restarts.
DATABASE_FOLDER = Path(__file__).parent.parent / "data" / "chroma"
COLLECTION_NAME = "hospital_documents"

# How many chunks to return for each question. 5 rather than 3: when a message mixes topics
# (e.g. a greeting with a name plus a booking request), the useful chunk can rank 4th.
TOP_K = 5

# Chunks further away than this are treated as unrelated (0 = same meaning, 1 = unrelated).
# Measured on data/evaluation/rag/questions.jsonl: answerable questions scored 0.17-0.52,
# unanswerable ones 0.53-0.62. 0.55 keeps every real answer; the LLM handles the borderline rest.
MAX_DISTANCE = 0.55


class KnowledgeError(Exception):
    """Raised when the knowledge base can't be searched (for example, Ollama isn't running)."""


@dataclass
class SearchResult:
    """One chunk found by search(), with how close it was to the question."""

    chunk_id: str
    doc_id: str
    title: str
    section: str
    text: str
    distance: float  # 0 = same meaning, 1 = unrelated


def embed(text: str) -> list[float]:
    """Turn one text into a vector (a list of 384 numbers) that represents its meaning."""
    response = httpx.post(
        OLLAMA_EMBED_URL,
        # truncate=False: refuse text that's too long instead of silently ignoring the end of it.
        json={"model": EMBEDDING_MODEL, "input": text, "truncate": False},
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["embeddings"][0]


def get_collection():
    """Open the collection (ChromaDB's word for a table) of chunks, creating it the first time."""
    client = chromadb.PersistentClient(path=str(DATABASE_FOLDER))
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=None,  # we create the embeddings ourselves, with Ollama
        configuration={"hnsw": {"space": "cosine"}},  # compare vectors with cosine similarity
    )


def sync_chunks(chunks: list[Chunk]) -> dict:
    """Make the database match the documents exactly: add new chunks, replace changed ones,
    and delete chunks that no longer exist. Safe to run any number of times."""
    if not chunks:
        # Without this check, a wrong folder path would make step 3 delete the whole knowledge base.
        raise ValueError("No chunks to save. Check that the documents folder is correct.")

    # 1. Embed every chunk BEFORE changing the database, so a problem leaves it untouched.
    vectors = []
    for chunk in chunks:
        try:
            vectors.append(embed(chunk.text))
        except httpx.HTTPStatusError as error:
            if "context length" in error.response.text:
                raise ValueError(
                    f"Chunk {chunk.chunk_id} is too long for the embedding model. "
                    "Split that section into smaller sections."
                ) from error
            raise

    # 2. Save: add new chunks, and replace existing ones that have the same chunk_id ("upsert").
    collection = get_collection()
    collection.upsert(
        ids=[chunk.chunk_id for chunk in chunks],
        embeddings=vectors,
        documents=[chunk.text for chunk in chunks],
        metadatas=[
            {"doc_id": chunk.doc_id, "title": chunk.title, "section": chunk.section, "status": chunk.status}
            for chunk in chunks
        ],
    )

    # 3. Delete chunks that are in the database but no longer in the documents.
    current_ids = {chunk.chunk_id for chunk in chunks}
    stored_ids = set(collection.get(include=[])["ids"])
    outdated_ids = stored_ids - current_ids
    if outdated_ids:
        collection.delete(ids=list(outdated_ids))

    return {"saved": len(chunks), "removed": len(outdated_ids), "total": collection.count()}


def search(question: str, top_k: int = TOP_K) -> list[SearchResult]:
    """Find the chunks most relevant to a question, most relevant first.

    Returns an empty list when nothing in the documents is close enough to the question.
    """
    try:
        question_vector = embed(question)
    except httpx.HTTPError as error:
        raise KnowledgeError("The embedding model could not be reached.") from error

    results = get_collection().query(
        query_embeddings=[question_vector],
        n_results=top_k,
        where={"status": "current"},  # never use outdated (superseded) documents
        include=["documents", "metadatas", "distances"],
    )
    documents, metadatas, distances = results["documents"], results["metadatas"], results["distances"]
    # ChromaDB's types allow these to be missing (when not requested). We requested them, so they're here.
    assert documents is not None and metadatas is not None and distances is not None

    # ChromaDB returns parallel lists, with one inner list per question. We asked one question: [0].
    found = []
    for chunk_id, text, metadata, distance in zip(results["ids"][0], documents[0], metadatas[0], distances[0]):
        if distance <= MAX_DISTANCE:
            found.append(SearchResult(
                chunk_id=chunk_id,
                doc_id=str(metadata["doc_id"]),  # str(): we only ever store text in these fields
                title=str(metadata["title"]),
                section=str(metadata["section"]),
                text=text,
                distance=round(distance, 3),
            ))
    return found
