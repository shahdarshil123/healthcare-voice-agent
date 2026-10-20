"""Tests for reading and chunking documents (app/documents.py). No AI model involved."""

from app.documents import chunk_document, load_all_chunks, split_front_matter

SAMPLE = """---
doc_id: sample-policy
title: Sample Policy
version: "1.0"
---

# Sample Policy

*Brightwater Community Hospital is fictional. This document is synthetic data for software development.*

An introduction paragraph.

## Fees

| Situation | Fee |
|---|---|
| Late | $25 |

## Waivers

Fees are waived in emergencies.
"""


def write_sample(tmp_path, text=SAMPLE, name="sample-policy.md"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_front_matter_is_separated_from_the_body():
    metadata, body = split_front_matter(SAMPLE)
    assert metadata["doc_id"] == "sample-policy"
    assert metadata["version"] == "1.0"
    assert "## Fees" in body


def test_table_separators_do_not_break_the_front_matter_split():
    _, body = split_front_matter(SAMPLE)
    assert "|---|---|" in body  # the "---" inside the table stays in the body


def test_one_chunk_per_section_plus_the_introduction(tmp_path):
    chunks = chunk_document(write_sample(tmp_path))
    assert [chunk.section for chunk in chunks] == ["Introduction", "Fees", "Waivers"]
    assert [chunk.chunk_id for chunk in chunks] == ["sample-policy#0", "sample-policy#1", "sample-policy#2"]


def test_chunks_start_with_title_and_heading_and_drop_the_boilerplate(tmp_path):
    fees = chunk_document(write_sample(tmp_path))[1]
    assert fees.text.startswith("Sample Policy\nFees\n\n")
    assert "$25" in fees.text
    intro = chunk_document(write_sample(tmp_path))[0]
    assert "fictional" not in intro.text and "# Sample Policy" not in intro.text


def test_documents_are_current_unless_marked_superseded(tmp_path):
    assert chunk_document(write_sample(tmp_path))[0].status == "current"
    old = SAMPLE.replace('version: "1.0"', 'version: "0.9"\nstatus: superseded')
    assert chunk_document(write_sample(tmp_path, old, "old.md"))[0].status == "superseded"


def test_real_documents_have_unique_chunk_ids_and_no_boilerplate():
    chunks = load_all_chunks()
    ids = [chunk.chunk_id for chunk in chunks]
    assert len(ids) == len(set(ids))
    assert not any("is fictional" in chunk.text for chunk in chunks)
    assert {chunk.status for chunk in chunks} == {"current", "superseded"}  # the archived policy is marked
