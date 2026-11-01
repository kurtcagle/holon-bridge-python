"""An in-memory RDF store that behaves like the bridge's Fuseki for the calls
the agent gateway makes.

Most stubs in this suite return canned rows for the query they expect. That is
the right tool for routes and ACL, and the wrong one for a *ledger*: the point
of the agent-call lifecycle is that a sequence of SPARQL updates leaves the
store in the right state, and a canned stub cannot tell a correct DELETE/INSERT
from a broken one. This runs the real statements against rdflib.

Identity and ACL questions are answered by ``IdentityStub`` first, as in the
other route tests, so the same knobs apply (``admin``, ``write_grant``...), plus
``denied_write_graphs`` / ``denied_replace_graphs`` to refuse a grant on
particular graphs.

``default_union=True`` makes the default graph the union of the named ones,
which is how the bridge's context queries (no ``GRAPH`` clause) are meant to see
domain data.
"""

from __future__ import annotations

import json

from rdflib import Dataset, URIRef

from ._identity import IdentityStub


class RdfStore(IdentityStub):
    def __init__(self) -> None:
        super().__init__()
        self.ds = Dataset(default_union=True)
        self.updates: list[str] = []
        self.denied_write_graphs: set[str] = set()
        self.denied_replace_graphs: set[str] = set()

    # --- the client surface ---------------------------------------------------------

    async def select(self, conn, query, *, default_graph=None):
        for graph in self.denied_write_graphs:
            if "grantsWrite" in query and graph in query and not self.admin:
                return {"boolean": False}
        for graph in self.denied_replace_graphs:
            if "grantsReplace" in query and graph in query and not self.admin:
                return {"boolean": False}
        infra = self.infra_select(query)
        if infra is not None:
            return infra
        result = self.ds.query(query)
        if result.type == "ASK":
            return {"boolean": bool(result.askAnswer)}
        return json.loads(result.serialize(format="json"))

    async def construct(self, conn, query, *, default_graph=None, timeout=None):
        result = self.ds.query(query)
        return result.graph.serialize(format="turtle")

    async def update(self, conn, update):
        self.updates.append(" ".join(update.split()))
        self.ds.update(update)

    async def post_graph(self, conn, graph_iri, turtle):
        self.ds.graph(URIRef(graph_iri)).parse(data=turtle, format="turtle")

    async def put_graph(self, conn, graph_iri, turtle):
        graph = self.ds.graph(URIRef(graph_iri))
        graph.remove((None, None, None))
        graph.parse(data=turtle, format="turtle")

    async def get_graph(self, conn, graph_iri):
        return self.ds.graph(URIRef(graph_iri)).serialize(format="turtle")

    async def drop_graph(self, conn, graph_iri):
        self.ds.remove_graph(self.ds.graph(URIRef(graph_iri)))

    async def aclose(self) -> None:  # the app closes its client at shutdown
        return None

    # --- test helpers ---------------------------------------------------------------

    def graph(self, iri: str):
        return self.ds.graph(URIRef(iri))

    def add_turtle(self, graph_iri: str, turtle: str) -> None:
        self.graph(graph_iri).parse(data=turtle, format="turtle")
