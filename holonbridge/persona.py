"""Persona existence and membership checks for switch_persona.

Takes a query_fn, the same injection shape acl.py uses (see its own
docstring for why): these are authorization-adjacent decisions, not data
retrieval, and this shape is what lets them run against the same
rdflib-backed unit-test harness test_acl.py / test_admin_bypass.py already
use, with FusekiClient.select wired in as query_fn only for production
(see deps.py's require_animus for that exact pattern -- routes/persona.py
does the same thing).

QueryFn/_run are duplicated from acl.py rather than imported from there:
acl.py's is a module-private helper (leading underscore), and four lines
of duplication is cheaper than depending on another module's private
surface -- same tradeoff conn.py's own docstring makes for
bank_scoped_datasets rather than a shared generic helper.

Two independent questions, kept independent on purpose -- see
switch_persona's route docstring for why collapsing them fails quietly:
does a Persona by this name exist at all in this dataset's ground truth
(persona_exists), and does the calling person hold a Home under it
(has_home, checked inside that person's own persona-user graph). Both
build their graph IRIs from Conn, never by hand -- see persona_scope.py's
module docstring for why that matters here too.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from .conn import Conn

HOLON = "https://w3id.org/holon/"

QueryFn = Callable[[str], "dict | Awaitable[dict]"]


async def _run(query_fn: QueryFn, query: str) -> dict:
    result = query_fn(query)
    if hasattr(result, "__await__"):
        result = await result  # type: ignore[assignment]
    return result  # type: ignore[return-value]


async def persona_exists(query_fn: QueryFn, conn: Conn, *, persona: str) -> bool:
    """Whether `persona` is a declared holon:Persona in this dataset's
    ground truth -- independent of whether the caller has any standing
    under it."""
    query = f"""PREFIX holon: <{HOLON}>
ASK {{ GRAPH <{conn.graph("holons")}> {{ <{conn.persona_graph(persona)}> a holon:Persona . }} }}"""
    result = await _run(query_fn, query)
    return bool(result.get("boolean"))


async def has_home(query_fn: QueryFn, conn: Conn, *, persona: str, person_id: str) -> bool:
    """Whether `person_id` holds a holon:Home inside their own graph under
    `persona` -- the membership test switch_persona gates on. Checked
    inside the person's own persona-user graph, not ground truth: a Home
    minted there is what "this person is inside this persona's envelope"
    means."""
    query = f"""PREFIX holon: <{HOLON}>
ASK {{
  GRAPH <{conn.persona_user_graph(persona, "holons", person_id)}> {{
    ?home a holon:Home ;
          holon:representsPerson <{person_id}> .
  }}
}}"""
    result = await _run(query_fn, query)
    return bool(result.get("boolean"))
