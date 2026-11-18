"""Structural freeze: an Inactive or Tombstoned holon cannot be rewritten by an ingest path.

Phase 1 (``lifecycle.write_block``) stops *fluent* changes. This closes the other
way in: a Turtle payload or a SPARQL update that names the holon, or one of its
fluents, as a subject.

What is checked, for a write that would change triples about ``subjects``:

* a subject that is an Inactive or Tombstoned **holon**           -> ``holon_inactive``
* a subject that is a **fluent** of such a holon                  -> ``holon_inactive``
* a subject that is a **lifecycle fluent**, or a payload that
  uses ``hb:lifecycleStatus`` as a predicate                      -> ``lifecycle_governed``
* a ``replace`` write whose target graph holds any such subject   -> ``holon_inactive``
  (a replace would rewrite it, whatever the payload says)
* a payload whose subjects cannot be read (RDF 1.2 passthrough)
  while any holon is Inactive or Tombstoned                       -> ``holon_inactive``

The last rule fails closed only when it matters: in a dataset with no inactive
holon an unreadable payload is allowed, so RDF 1.2 writes are not penalised.

Covered: everything that goes through ``ingest.write_turtle_to_graph`` (``/holon``,
``/graph/push``, ``/ingest``, ``/message/create``, trigger rule writes) and
``/sparql/update``.

Not covered, and said plainly so nobody takes this for a seal:

* a SPARQL update whose subjects come from its WHERE clause, and whole-graph
  operations (CLEAR, DROP, LOAD, COPY, MOVE, ADD) -- ``update_subjects`` reports
  the update as incomplete and only the subjects it can list are checked;
* named-rule writes and ``/graph-op``, which build their own updates;
* the lifecycle route itself, which writes its own fluent directly.

Those stay an access-control matter.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import HTTPException

from .conn import Conn
from .fuseki import FusekiClient
from .lifecycle import _PREFIXES, LIFECYCLE_PROPERTY, state_name

__all__ = ["StructuralBlock", "check_structural", "structural_block"]

_UNSAFE_IRI = re.compile(r'[\s<>"{}|^`\\]')

_CHUNK = 400  # subjects per VALUES clause
_REPORT = 20  # most subjects named in a refusal


@dataclass(frozen=True)
class StructuralBlock:
    """Why a write was refused. ``kind`` is ``governed``, ``inactive``, ``replace`` or ``unreadable``."""

    kind: str
    subjects: tuple[str, ...] = ()
    holders: tuple[str, ...] = ()
    state: str | None = None

    @property
    def error(self) -> str:
        return "lifecycle_governed" if self.kind == "governed" else "holon_inactive"

    @property
    def message(self) -> str:
        who = ", ".join(f"<{h}>" for h in self.holders) or "an inactive holon"
        if self.kind == "governed":
            return (
                "this write touches a holon's lifecycle state; it changes only through "
                "POST /holon/lifecycle"
            )
        if self.kind == "replace":
            if not self.state:
                return (
                    "a replace would rewrite holon lifecycle state held in this graph; "
                    "use merge, or write to another graph"
                )
            return (
                f"a replace would rewrite {who}, which is {self.state}: an inactive holon can be "
                "read but not changed. Use merge for other data, or make it Active first "
                "(POST /holon/lifecycle)"
            )
        if self.kind == "unreadable":
            return (
                "the payload's subjects cannot be read here (RDF 1.2), and a holon in this dataset "
                "is Inactive or Tombstoned. Send it as Turtle 1.1, or make the holon Active first"
            )
        return (
            f"this write changes {who}, which is {self.state}: an inactive holon can be read but "
            "not changed. Make it Active first (POST /holon/lifecycle)"
        )

    def detail(self) -> dict:
        out: dict = {"error": self.error, "message": self.message, "kind": self.kind}
        if self.subjects:
            out["subjects"] = list(self.subjects[:_REPORT])
        if self.holders:
            out["holders"] = list(self.holders[:_REPORT])
        if self.state:
            out["state"] = self.state
        return out


def _matcher(conn: Conn) -> str:
    """Patterns binding ``?s`` (the subject under test) to why it is blocked.

    ``?kind`` is ``governed`` or ``inactive``; ``?holder`` and ``?state`` say which holon.
    """
    return f"""{{
    GRAPH <{conn.holons_graph}> {{ ?s holon:fluentProperty hb:lifecycleStatus . }}
    BIND("governed" AS ?kind)
  }} UNION {{
    GRAPH <{conn.holons_graph}> {{ ?s hb:lifecycleStatus ?lifecycle . }}
    GRAPH <{conn.scene_graph}> {{ ?lifecycle holon:currentValue ?state . }}
    FILTER (?state IN (hb:Inactive, hb:Tombstoned))
    BIND(?s AS ?holder)
    BIND("inactive" AS ?kind)
  }} UNION {{
    GRAPH <{conn.holons_graph}> {{
      ?s holon:fluentProperty ?fluentProperty .
      ?holder ?link ?s .
      ?holder hb:lifecycleStatus ?lifecycle .
    }}
    GRAPH <{conn.scene_graph}> {{ ?lifecycle holon:currentValue ?state . }}
    FILTER (?state IN (hb:Inactive, hb:Tombstoned))
    BIND("inactive" AS ?kind)
  }}"""


async def _rows(client: FusekiClient, conn: Conn, source: str) -> list[dict]:
    query = f"""{_PREFIXES}
SELECT DISTINCT ?s ?kind ?holder ?state WHERE {{
  {source}
  {_matcher(conn)}
}} LIMIT {_REPORT * 5}"""
    return (await client.select(conn, query)).get("results", {}).get("bindings", [])


def _block(rows: list[dict], *, replace: bool = False) -> StructuralBlock | None:
    """Fold query rows into one refusal. In a replace scan every row blocks, even a
    lifecycle fluent: a replace rewrites the whole graph, so it would drop them."""
    if not rows:
        return None
    subjects = tuple(sorted({r["s"]["value"] for r in rows}))
    governed = any(r["kind"]["value"] == "governed" for r in rows)
    if governed and not replace:
        return StructuralBlock("governed", subjects)
    holders = tuple(sorted({r["holder"]["value"] for r in rows if "holder" in r}))
    states = {state_name(r["state"]["value"]) for r in rows if "state" in r}
    state = "Tombstoned" if "Tombstoned" in states else "Inactive" if states else None
    return StructuralBlock("replace" if replace else "inactive", subjects, holders, state)


async def _any_inactive(client: FusekiClient, conn: Conn) -> bool:
    query = f"""{_PREFIXES}
ASK {{
  GRAPH <{conn.holons_graph}> {{ ?holder hb:lifecycleStatus ?lifecycle . }}
  GRAPH <{conn.scene_graph}> {{ ?lifecycle holon:currentValue ?state . }}
  FILTER (?state IN (hb:Inactive, hb:Tombstoned))
}}"""
    return bool((await client.select(conn, query)).get("boolean"))


async def structural_block(
    client: FusekiClient,
    conn: Conn,
    *,
    subjects: Iterable[str] | None,
    predicates: Iterable[str] | None = None,
    raw_text: str | None = None,
    graph: str | None = None,
    replace: bool = False,
) -> StructuralBlock | None:
    """``None`` if the write may go ahead, otherwise why not.

    ``subjects`` is ``None`` when the payload could not be read. ``predicates`` and
    ``raw_text`` are only used to catch ``hb:lifecycleStatus``. ``graph`` and
    ``replace`` together mean "this write replaces ``graph``". Errors from Fuseki
    propagate: failing open would unfreeze every holon exactly when the store
    cannot be read.
    """
    if predicates is not None and LIFECYCLE_PROPERTY in set(predicates):
        return StructuralBlock("governed")

    if subjects is None:
        if raw_text and "lifecycleStatus" in raw_text:
            return StructuralBlock("governed")
        if replace and graph:
            hit = _block(
                await _rows(client, conn, f"GRAPH <{graph}> {{ ?s ?sp ?so . }}"), replace=True
            )
            if hit:
                return hit
        if await _any_inactive(client, conn):
            return StructuralBlock("unreadable")
        return None

    if replace and graph:
        hit = _block(await _rows(client, conn, f"GRAPH <{graph}> {{ ?s ?sp ?so . }}"), replace=True)
        if hit:
            return hit

    # An IRI that could not appear inside <...> cannot name a holon either.
    names = sorted(i for i in set(subjects) if not _UNSAFE_IRI.search(i))
    for start in range(0, len(names), _CHUNK):
        values = " ".join(f"<{iri}>" for iri in names[start : start + _CHUNK])
        hit = _block(await _rows(client, conn, f"VALUES ?s {{ {values} }}"))
        if hit:
            return hit
    return None


async def check_structural(
    client: FusekiClient,
    conn: Conn,
    *,
    subjects: Iterable[str] | None,
    **kwargs,
) -> None:
    """Raise a 409 ``HTTPException`` if :func:`structural_block` refuses the write."""
    blocked = await structural_block(client, conn, subjects=subjects, **kwargs)
    if blocked:
        raise HTTPException(409, detail=blocked.detail())
