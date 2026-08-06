from __future__ import annotations

import pytest

from holonbridge.databook import DataBook


def test_primary_graph_block_prefers_turtle():
    db = DataBook.parse("---\nid: x\n---\n\n```turtle\n<urn:a> <urn:b> <urn:c> .\n```\n")
    assert db.primary_graph_block().body.strip() == "<urn:a> <urn:b> <urn:c> ."


def test_primary_graph_block_accepts_turtle12():
    db = DataBook.parse("---\nid: x\n---\n\n```turtle12\n<urn:a> <urn:b> <urn:c> .\n```\n")
    assert db.primary_graph_block().lang == "turtle12"


def test_primary_graph_block_skips_non_rdf_blocks():
    db = DataBook.parse(
        "---\nid: x\n---\n\n```sparql\nSELECT * WHERE { ?s ?p ?o }\n```\n\n"
        "```turtle\n<urn:a> <urn:b> <urn:c> .\n```\n"
    )
    assert db.primary_graph_block().lang == "turtle"


def test_primary_graph_block_raises_when_none_found():
    db = DataBook.parse("---\nid: x\n---\nJust prose.\n")
    with pytest.raises(ValueError, match="no turtle"):
        db.primary_graph_block()


def test_named_graph_from_frontmatter():
    db = DataBook.parse("---\nid: x\ngraph:\n  named_graph: urn:ds:sensor-grid\n---\n")
    assert db.named_graph == "urn:ds:sensor-grid"


def test_named_graph_absent_is_none():
    db = DataBook.parse("---\nid: x\n---\n")
    assert db.named_graph is None
