"""Tests for the persona/user graph-naming methods added to Conn 2026-08-15.

conn.py imports holonbridge.config, which isn't part of this deliverable --
these tests exercise Conn directly via its dataclass fields rather than
constructing it through resolve_conn, so they don't need that module.
"""

from __future__ import annotations

import unittest

from holonbridge.conn import Conn


def make_conn(**overrides) -> Conn:
    defaults = dict(
        base_url="http://localhost:3030",
        dataset="causalspark",
        overridden=False,
        bank_name="local",
    )
    defaults.update(overrides)
    return Conn(**defaults)


class PersonaGraphNamingTests(unittest.TestCase):
    def test_persona_user_graph_matches_the_live_convention(self) -> None:
        conn = make_conn()
        self.assertEqual(
            conn.persona_user_graph("aimee", "holons", "caroline"),
            "urn:causalspark:persona:aimee:user:caroline:holons",
        )

    def test_persona_graph(self) -> None:
        conn = make_conn()
        self.assertEqual(conn.persona_graph("aimee"), "urn:causalspark:persona:aimee")

    def test_persona_for_graph_round_trips(self) -> None:
        conn = make_conn()
        g = conn.persona_user_graph("carlo", "holons", "public")
        self.assertEqual(conn.persona_for_graph(g), conn.persona_graph("carlo"))

    def test_org_tier_graphs_are_not_persona_gated(self) -> None:
        conn = make_conn()
        for graph in (conn.ontology_graph, conn.holons_graph, conn.shapes_graph):
            self.assertIsNone(conn.persona_for_graph(graph))

    def test_unrelated_graph_returns_none(self) -> None:
        conn = make_conn()
        self.assertIsNone(conn.persona_for_graph("urn:something:unrelated"))

    def test_rejects_unsafe_characters(self) -> None:
        conn = make_conn()
        with self.assertRaises(ValueError):
            conn.persona_user_graph("a:b", "holons", "x")
        with self.assertRaises(ValueError):
            conn.persona_graph("a b")

    def test_rejects_unknown_role(self) -> None:
        conn = make_conn()
        with self.assertRaises(ValueError):
            conn.persona_user_graph("aimee", "not-a-real-role", "kurt")

    def test_bank_scoped_convention(self) -> None:
        conn = make_conn(bank_scoped_datasets=frozenset({"causalspark"}))
        self.assertEqual(
            conn.persona_user_graph("aimee", "holons", "kurt"),
            "urn:local:causalspark:persona:aimee:user:kurt:holons",
        )


class PersonSlugTests(unittest.TestCase):
    """person_slug added 2026-08-17: persona_user_graph's `user` argument
    is a short slug, but Animus.person (what every real caller actually
    has) is a full Person IRI -- see conn.py's own CHANGED note and
    persona_scope.py / persona.py for the bug this was fixing."""

    def test_derives_the_trailing_segment(self) -> None:
        self.assertEqual(Conn.person_slug("urn:causalspark:person:kurt"), "kurt")

    def test_works_regardless_of_bank_scoping_prefix(self) -> None:
        # The prefix ahead of the local segment never matters -- only the
        # trailing segment does.
        self.assertEqual(
            Conn.person_slug("urn:local:causalspark:person:caroline"), "caroline"
        )

    def test_feeds_persona_user_graph_without_raising(self) -> None:
        conn = make_conn()
        slug = conn.person_slug("urn:causalspark:person:kurt")
        self.assertEqual(
            conn.persona_user_graph("aimee", "holons", slug),
            "urn:causalspark:persona:aimee:user:kurt:holons",
        )


if __name__ == "__main__":
    unittest.main()
