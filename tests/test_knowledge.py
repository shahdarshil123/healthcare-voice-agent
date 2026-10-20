"""Tests for search() in app/knowledge.py, with a fake embedding model and a fake ChromaDB collection."""

# Fixtures are passed by parameter name, and the fakes only need the one method search() calls.
# pylint: disable=redefined-outer-name,unused-argument,too-few-public-methods,missing-class-docstring

import json
import logging

import httpx
import pytest
from chromadb.errors import ChromaError

from app import knowledge
from app.knowledge import KnowledgeError, search


class FakeCollection:
    """Returns fixed nearest chunks, like ChromaDB's query() would."""

    def __init__(self, distances):
        self.distances = distances

    def query(self, **kwargs):
        n = len(self.distances)
        metadata = [{"doc_id": "doc", "title": "Doc", "section": f"S{i}", "status": "current"} for i in range(n)]
        return {
            "ids": [[f"doc#{i}" for i in range(n)]],
            "documents": [[f"text {i}" for i in range(n)]],
            "metadatas": [metadata],
            "distances": [self.distances],
        }


@pytest.fixture
def fake_embedding(monkeypatch):
    monkeypatch.setattr(knowledge, "embed", lambda text: [0.0] * 384)


def test_chunks_beyond_the_threshold_are_dropped(monkeypatch, fake_embedding):
    monkeypatch.setattr(knowledge, "get_collection", lambda: FakeCollection([0.2, 0.5, 0.6, 0.9]))

    results = search("question")

    assert [result.chunk_id for result in results] == ["doc#0", "doc#1"]  # 0.6 and 0.9 > MAX_DISTANCE
    assert results[0].distance == 0.2


def test_every_candidate_is_logged_including_dropped_ones(monkeypatch, fake_embedding, caplog):
    monkeypatch.setattr(knowledge, "get_collection", lambda: FakeCollection([0.2, 0.9]))

    with caplog.at_level(logging.INFO, logger="carevoice.retrieval"):
        search("my secret question")

    record = json.loads(caplog.records[-1].getMessage())
    assert record["event"] == "retrieval"
    assert [c["kept"] for c in record["candidates"]] == [True, False]
    assert record["kept"] == 1
    assert "my secret question" not in caplog.text  # the user's words are never logged


def test_embedding_failure_becomes_a_knowledge_error(monkeypatch):
    def refuse(text):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(knowledge, "embed", refuse)
    with pytest.raises(KnowledgeError):
        search("question")


def test_database_failure_becomes_a_knowledge_error(monkeypatch, fake_embedding):
    class BrokenCollection:
        def query(self, **kwargs):
            raise ChromaError("database is corrupted")

    monkeypatch.setattr(knowledge, "get_collection", BrokenCollection)
    with pytest.raises(KnowledgeError):
        search("question")


def test_sync_refuses_an_empty_chunk_list():
    with pytest.raises(ValueError, match="No chunks"):
        knowledge.sync_chunks([])
