"""Load the hospital documents into the knowledge base (ChromaDB).

Run this whenever a document in data/documents/ is added, changed, or deleted.
It's safe to run again: the knowledge base is updated to match the files exactly.

Usage:
    uv run python ingest.py
"""

from app.documents import load_all_chunks
from app.knowledge import sync_chunks

chunks = load_all_chunks()
print(f"Read {len(chunks)} chunks from the documents. Embedding and saving...")

result = sync_chunks(chunks)
print(f"Saved {result['saved']} chunks and removed {result['removed']} outdated ones.")
print(f"The knowledge base now holds {result['total']} chunks.")
