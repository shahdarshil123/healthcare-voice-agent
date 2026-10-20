"""Reading the hospital documents and cutting them into chunks for search.

Each document is a Markdown file that starts with front matter (metadata between "---" lines).
Every "## " section becomes one chunk, because each section covers one topic.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

DOCUMENTS_FOLDER = Path(__file__).parent.parent / "data" / "documents"

# Every document repeats this line. It says nothing about the topic, so we leave it out of chunks.
FICTIONAL_NOTICE = "*Brightwater Community Hospital is fictional."


@dataclass
class Chunk:
    """One searchable piece of a document, usually one "## " section."""

    chunk_id: str  # e.g. "cancellation-policy#4". The same section always gets the same ID.
    doc_id: str    # which document it came from, e.g. "cancellation-policy"
    title: str     # the document's title, used when citing sources
    section: str   # the section heading, e.g. "Fees"
    status: str    # "current", or "superseded" for an outdated document
    text: str      # what gets embedded, searched, and shown to the model


def split_front_matter(file_text: str) -> tuple[dict, str]:
    """Separate the metadata between the first two "---" lines from the rest of the document."""
    _, front_matter, body = file_text.split("---", 2)
    return yaml.safe_load(front_matter), body


def chunk_document(path: Path) -> list[Chunk]:
    """Cut one document into chunks: the introduction, then one chunk per "## " section."""
    metadata, body = split_front_matter(path.read_text(encoding="utf-8"))
    pieces = body.split("\n## ")

    chunks = []
    for index, piece in enumerate(pieces):
        if index == 0:
            # Before the first "## ": the "# Title" line, the fictional notice, and maybe an introduction.
            section = "Introduction"
            lines = [line for line in piece.splitlines()
                     if not line.startswith("# ") and not line.startswith(FICTIONAL_NOTICE)]
            content = "\n".join(lines).strip()
        else:
            # The first line is the heading; everything after it is the section's content.
            section, _, content = piece.partition("\n")
            content = content.strip()

        if not content:
            continue

        chunks.append(Chunk(
            chunk_id=f"{metadata['doc_id']}#{index}",
            doc_id=metadata["doc_id"],
            title=metadata["title"],
            section=section.strip(),
            status=metadata.get("status", "current"),
            # Starting with the title and heading keeps each chunk understandable on its own.
            text=f"{metadata['title']}\n{section.strip()}\n\n{content}",
        ))
    return chunks


def load_all_chunks(folder: Path = DOCUMENTS_FOLDER) -> list[Chunk]:
    """Chunk every document in the folder, including subfolders such as "archive"."""
    chunks = []
    for path in sorted(folder.rglob("*.md")):
        chunks.extend(chunk_document(path))
    return chunks
