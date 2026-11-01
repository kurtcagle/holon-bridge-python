"""The ``hb:AgentCall`` ledger: ``urn:{dataset}:agent-calls``.

One node per call, ``urn:{dataset}:agent-call:{id}``. Reads and writes are kept
here so the lifecycle code in ``service.py`` talks about calls, not triples.

Updates that change a call's state replace the affected predicates in a single
request (delete then insert, joined by ``;``), so a reader never sees a call
with two statuses. Whether a *transition* is allowed is decided by the caller
holding the dataset write lock; this module only stores.
"""

from __future__ import annotations

import re
from dataclasses import fields
from typing import Any

from ..conn import Conn
from ..fuseki import FusekiClient, FusekiError
from ..rdfutil import collect
from ..turtle import literal
from .model import CALL_CLASS_SUFFIX, HB, Call

_XSD = "http://www.w3.org/2001/XMLSchema#"
_SAFE_IRI = re.compile(r'^[A-Za-z][A-Za-z0-9+.\-]*:[^\s<>"{}|\\^`]*$')

# attribute -> (predicate local name, kind). ``iri`` kinds are written as IRIs
# when they are usable as one and as plain literals otherwise, so a hand-edited
# value can never break out of the update it is spliced into.
_PROPS: dict[str, tuple[str, str]] = {
    "id": ("id", "str"),
    "endpoint_id": ("agentEndpoint", "str"),
    "status": ("callStatus", "str"),
    "requested_by": ("requestedBy", "iri"),
    "requested_at": ("requestedAt", "dt"),
    "task": ("task", "str"),
    "body": ("requestBody", "str"),
    "digest": ("requestDigest", "str"),
    "target_graph": ("targetGraph", "iri"),
    "focus": ("focus", "iri"),
    "via_trigger": ("viaTrigger", "iri"),
    "via_pipeline": ("viaPipeline", "str"),
    "via_stage": ("viaStage", "str"),
    "approval_mode": ("approvalMode", "str"),
    "approved_by": ("approvedBy", "iri"),
    "approved_at": ("approvedAt", "dt"),
    "started_at": ("startedAt", "dt"),
    "completed_at": ("completedAt", "dt"),
    "attempts": ("attempts", "int"),
    "http_status": ("httpStatus", "int"),
    "response_summary": ("responseSummary", "str"),
    "response_bytes": ("responseBytes", "int"),
    "error": ("error", "str"),
    "candidate": ("candidate", "iri"),
    "rejected_reason": ("rejectedReason", "str"),
    "rejected_by": ("rejectedBy", "iri"),
}
_BY_PREDICATE = {predicate: attr for attr, (predicate, _kind) in _PROPS.items()}
_CALL_ATTRS = {f.name for f in fields(Call)}
assert set(_PROPS) <= _CALL_ATTRS, set(_PROPS) - _CALL_ATTRS


def call_node(conn: Conn, call_id: str) -> str:
    """``urn:{dataset}:agent-call:{id}``."""
    return conn.scoped("agent-calls", call_id)


def _term(value: Any, kind: str) -> str:
    if kind == "int":
        return literal(str(int(value)), datatype=f"<{_XSD}integer>")
    if kind == "dt":
        return literal(str(value), datatype=f"<{_XSD}dateTime>")
    if kind == "iri" and isinstance(value, str) and _SAFE_IRI.match(value):
        return f"<{value}>"
    return literal(str(value))


def _pairs(values: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for attr, value in values.items():
        predicate, kind = _PROPS[attr]
        if value in (None, ""):
            continue
        out.append(f"<{HB}{predicate}> {_term(value, kind)}")
    return out


class CallStore:
    def __init__(self, client: FusekiClient) -> None:
        self._client = client

    async def put(self, conn: Conn, call: Call) -> None:
        """Insert a new call. Refuses to overwrite: ids are minted, not chosen."""
        graph = conn.graph("agent-calls")
        node = call_node(conn, call.id)
        values = {attr: getattr(call, attr) for attr in _PROPS}
        pairs = [f"a <{HB}{CALL_CLASS_SUFFIX}>"] + _pairs(values)
        await self._client.update(
            conn,
            f"""INSERT DATA {{ GRAPH <{graph}> {{
<{node}>
    {" ;\n    ".join(pairs)} .
}} }}""",
        )

    async def replace(self, conn: Conn, call: Call, **changes: Any) -> None:
        """Set the given attributes on a stored call (and on ``call``).

        An attribute set to ``""`` or ``None`` is cleared, not stored empty.
        """
        unknown = set(changes) - set(_PROPS)
        if unknown:
            raise KeyError(f"not a call attribute: {', '.join(sorted(unknown))}")
        graph = conn.graph("agent-calls")
        node = call_node(conn, call.id)
        predicates = " ".join(f"<{HB}{_PROPS[attr][0]}>" for attr in changes)
        insert = _pairs(changes)
        statement = f"""DELETE {{ GRAPH <{graph}> {{ <{node}> ?p ?o }} }}
WHERE {{ GRAPH <{graph}> {{ <{node}> ?p ?o . VALUES ?p {{ {predicates} }} }} }}"""
        if insert:
            statement += f""" ;
INSERT DATA {{ GRAPH <{graph}> {{
<{node}> {" ;\n    ".join(insert)} .
}} }}"""
        await self._client.update(conn, statement)
        for attr, value in changes.items():
            setattr(call, attr, value if value is not None else _empty(attr))

    # --- reading ------------------------------------------------------------------

    async def get(self, conn: Conn, call_id: str) -> Call | None:
        graph = conn.graph("agent-calls")
        node = call_node(conn, call_id)
        rows = (
            await self._client.select(
                conn,
                f"""SELECT ?c ?p ?o WHERE {{ GRAPH <{graph}> {{ VALUES ?c {{ <{node}> }} ?c ?p ?o }} }}""",
            )
        )["results"]["bindings"]
        calls = _calls(rows)
        return calls[0] if calls else None

    async def list_calls(
        self,
        conn: Conn,
        *,
        status: str | None = None,
        endpoint: str | None = None,
        limit: int = 100,
    ) -> list[Call]:
        graph = conn.graph("agent-calls")
        try:
            rows = (
                await self._client.select(
                    conn,
                    f"""SELECT ?c ?p ?o
WHERE {{
  GRAPH <{graph}> {{
    ?c a ?type .
    FILTER( STRENDS(STR(?type), "{CALL_CLASS_SUFFIX}") )
    ?c ?p ?o .
  }}
}}""",
                )
            )["results"]["bindings"]
        except (FusekiError, KeyError):
            return []
        calls = _calls(rows)
        if status:
            calls = [c for c in calls if c.status == status]
        if endpoint:
            calls = [c for c in calls if c.endpoint_id == endpoint]
        calls.sort(key=lambda c: (c.requested_at, c.id), reverse=True)
        return calls[: max(limit, 0)]

    async def open_call_exists(self, conn: Conn, *, trigger_iri: str, focus: str) -> bool:
        """Is a call for this (trigger, focus) still able to leave the bridge?"""
        graph = conn.graph("agent-calls")
        result = await self._client.select(
            conn,
            f"""ASK {{ GRAPH <{graph}> {{
  ?c <{HB}viaTrigger> <{trigger_iri}> ;
     <{HB}focus> <{focus}> ;
     <{HB}callStatus> ?s .
  FILTER( ?s IN ("PendingApproval", "Approved", "Sending") )
}} }}""",
        )
        return bool(result.get("boolean"))


def _empty(attr: str) -> Any:
    """What a cleared attribute reads as: the dataclass default."""
    if attr == "attempts":
        return 0
    return None if _PROPS[attr][1] == "int" else ""


def _calls(rows: list[dict[str, Any]]) -> list[Call]:
    calls: list[Call] = []
    for iri, props in collect(rows, "c").items():
        values: dict[str, Any] = {}
        for predicate, attr in _BY_PREDICATE.items():
            raw = props.get(predicate)
            if not raw:
                continue
            kind = _PROPS[attr][1]
            if kind == "int":
                try:
                    values[attr] = int(raw[0])
                except ValueError:
                    continue
            else:
                values[attr] = raw[0]
        if "id" not in values:
            values["id"] = iri.rsplit(":", 1)[-1]
        calls.append(Call(iri=iri, **values))
    return calls
