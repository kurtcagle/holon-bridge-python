"""Named-graph routes: listing, retrieval, push, and drop.

``push`` is the ingestion path and the only place the SHACL gate is armed.
When ``SHACL_REQUIRED`` is on, the shapes graph defaults to the dataset's own
``urn:{dataset}:shacl`` — which is precisely why the naming convention has to
hold. Delta mode is on by default so an existing violation elsewhere in the
target graph cannot reject an unrelated write.

CHANGED 2026-08-17: ``push`` now depends on ``AnimusDep`` and gates every
call -- ``mode=merge`` requires ``check_write`` (an explicit
``holon:grantsWrite``), ``mode=replace`` requires the stricter, independent
``check_replace`` (``holon:grantsReplace``). Before this the route had no
ACL check of any kind, unlike ``/sparql/*`` (gated since 2026-08-15).
``grantsWrite`` does NOT imply ``grantsReplace`` -- appends are the norm,
wholesale graph overwrite is the deliberately-rare exception; see
``holonbridge.acl.check_replace`` for the reasoning. ``/ingest`` was gated
2026-08-29 (see ``routes/pipeline.py``'s docstring); ``/graph`` (DROP) was
gated 2026-08-31 -- see the CHANGED note below.

CHANGED 2026-08-28: the ACL check, SHACL gate, and GSP write themselves
moved into ``holonbridge.ingest.write_turtle_to_graph``, shared with
``POST /holon`` (create_holon) and ``POST /message/create``
(create_message) -- see that module's docstring. This route now only owns
the parts specific to a raw Turtle push: the optional local pre-parse and
unpacking the request body. Behaviour is unchanged; this is a pure
extraction.

CHANGED 2026-08-31: ``DELETE /graph`` (``drop_graph``) now depends on
``AnimusDep`` and requires ``check_replace`` on the graph being dropped --
the other half of the ``HB_ACL_REMAINING_ENDPOINTS-01`` finding (the
``/graph-op`` half was fixed the same day; see ``routes/named_rules.py``,
whose ``clear``/``drop``/``copy`` operations check the identical grant for
the identical reason: DROP is unambiguously destructive and wholesale, the
same shape ``drop_pipeline`` and ``check_replace`` everywhere else in this
codebase already treat that way). Before this, ``drop_graph`` had zero ACL
check of any kind.

CHANGED 2026-09-30: ``GET /graph`` now depends on ``AnimusDep`` and applies
exactly the read rules ``/sparql/select`` applies, via
``acl.authorize_query`` -- a persona graph needs a ReadGrant, an admin
bypass or a read delegation; the audit graph is admin-only; ground truth is
readable by any identified caller. Before this the route had no identity
check at all, so anyone holding the shared bearer token could read any
graph's full contents: other people's private persona graphs, delegations,
grants and the audit log. It is a behaviour change for callers that send
only the bearer token (they now get a 401 like every other identity-gated
route). ``GET /graphs`` (names and triple counts only) is unchanged.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, model_validator

from .. import audit
from .. import delegation as dg
from ..acl import authorize_query, check_replace, extract_graph_refs
from ..deps import AnimusDep, ClientDep, ConnDep, SettingsDep
from ..fuseki import JSONLD, FusekiError
from ..jsonld import JsonLdError, json_ld_to_turtle, render, settings_for, to_http
from ..trigger_hook import after_write
from ..ingest import write_turtle_to_graph
from ..turtle import TurtleSyntaxError, parse

router = APIRouter(tags=["graphs"])

_LIST_QUERY = """SELECT ?g (COUNT(*) AS ?triples)
WHERE { GRAPH ?g { ?s ?p ?o } }
GROUP BY ?g
ORDER BY ?g"""


class PushRequest(BaseModel):
    turtle: str | None = Field(
        default=None,
        min_length=1,
        description="the payload (Turtle, or JSON-LD when format is json-ld)",
    )
    data: str | None = Field(
        default=None,
        min_length=1,
        description="synonym for turtle, for a payload that is not Turtle",
    )
    format: Literal["turtle", "json-ld"] = Field(
        default="turtle",
        description=(
            "serialisation of the payload. json-ld is converted to Turtle by the "
            "bridge (default graph only; remote @context refused)"
        ),
    )
    graph_iri: str = Field(..., min_length=1)
    shapes_graph: str | None = None
    mode: str = Field(default="merge", pattern="^(merge|replace)$")
    reduction_rule_id: str | None = Field(
        default=None,
        description=(
            "Reduce the candidate write to its current state, via this "
            "registered named rule, before validating it. Mechanism only — "
            "see holonbridge.shacl._apply_reduction for what the rule's "
            "CONSTRUCT needs to look like."
        ),
    )

    @model_validator(mode="after")
    def _one_payload(self) -> PushRequest:
        if (self.turtle is None) == (self.data is None):
            raise ValueError("send the payload in exactly one of 'turtle' or 'data'")
        return self

    @property
    def payload(self) -> str:
        return self.turtle if self.turtle is not None else self.data  # type: ignore[return-value]


@router.get("/graphs")
async def list_graphs(
    conn: ConnDep,
    client: ClientDep,
    filter: str | None = Query(default=None, description="substring match on graph IRI"),
) -> dict:
    try:
        results = await client.select(conn, _LIST_QUERY)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    graphs = [
        {"graph": row["g"]["value"], "triples": int(row["triples"]["value"])}
        for row in results.get("results", {}).get("bindings", [])
    ]
    if filter:
        graphs = [g for g in graphs if filter in g["graph"]]

    return {
        "dataset": conn.dataset,
        "overridden": conn.overridden,
        "count": len(graphs),
        "graphs": graphs,
    }


@router.get("/graph", response_class=PlainTextResponse)
async def get_graph(
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    request: Request,
    iri: str = Query(...),
    format: Literal["turtle", "json-ld"] = Query(default="turtle"),
    jsonld_form: Literal["compact", "expanded"] = Query(default="compact"),
) -> Response:
    """Return one named graph as Turtle, under the same read rules as
    ``POST /sparql/select`` -- see the module docstring's 2026-09-30 note.

    The check reuses ``acl.authorize_query`` on a probe query for this one
    graph. ``iri`` comes from the caller and is interpolated into that
    probe, so it must be a plain IRI (``delegation.safe_iri``) and the probe
    must parse to *exactly* that single graph reference; anything else is
    refused rather than checked. Without that, a crafted ``iri`` could make
    the probe reference a different graph from the one actually fetched.
    """
    try:
        safe = dg.safe_iri(iri)
    except dg.DelegationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    audit.annotate(graphs=[safe])
    probe = f"SELECT * WHERE {{ GRAPH <{safe}> {{ ?s ?p ?o }} }}"
    if extract_graph_refs(probe) != {safe}:
        audit.annotate(reason="graph IRI could not be read as a single graph reference")
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="graph IRI could not be read as a single graph reference",
        )

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    decision = await authorize_query(
        query_fn,
        conn.holons_graph,
        person=animus.person,
        sparql_text=probe,
        persona_of_graph=conn.persona_for_graph,
    )
    if not decision.allowed:
        audit.annotate(reason=decision.reason)
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=decision.reason)
    if animus.person and audit.foreign_user_graphs([safe], conn.person_slug(animus.person)):
        audit.annotate(
            security_read=True, reason="read of another person's private persona graph"
        )

    try:
        rendered = await render(
            lambda: client.get_graph(conn, safe),
            lambda: client.get_graph(conn, safe, accept=JSONLD),
            fmt=format,
            form=jsonld_form,
            settings=settings_for(request),
        )
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    except JsonLdError as exc:
        raise to_http(exc) from exc
    headers = {"X-JSONLD-Engine": rendered.engine} if rendered.engine else None
    media = rendered.media_type if format == "json-ld" else "text/plain"
    return PlainTextResponse(rendered.text, media_type=media, headers=headers)


@router.post("/graph/push")
async def push_turtle(
    body: PushRequest, conn: ConnDep, client: ClientDep, settings: SettingsDep, animus: AnimusDep
) -> dict:
    if body.format == "json-ld":
        # Converted once, here, so the single GSP write path stays Turtle.
        try:
            turtle_payload = await json_ld_to_turtle(body.payload, settings=settings)
        except JsonLdError as exc:
            raise to_http(exc) from exc
    else:
        turtle_payload = body.payload
        # Optional local pre-parse. Off by default: rdflib cannot read Turtle 1.2,
        # so Jena stays the syntax authority unless the operator opts in.
        if settings.parse_mode == "local":
            try:
                parse(turtle_payload)
            except TurtleSyntaxError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return await write_turtle_to_graph(
        turtle=turtle_payload,
        graph_iri=body.graph_iri,
        mode=body.mode,
        shapes_graph=body.shapes_graph,
        reduction_rule_id=body.reduction_rule_id,
        conn=conn,
        client=client,
        shacl_required=settings.shacl_required,
        shacl_delta=settings.shacl_delta,
        animus=animus,
    )


@router.delete("/graph")
async def drop_graph(
    conn: ConnDep, client: ClientDep, animus: AnimusDep, iri: str = Query(...)
) -> dict:
    """Drop a named graph entirely.

    Gated by ``check_replace`` -- DROP is unambiguously destructive and
    wholesale, the same reasoning that makes ``grantsReplace`` (not
    ``grantsWrite``) the grant every other DROP-shaped operation in this
    codebase requires: ``drop_pipeline`` (routes/pipeline.py) and the
    ``drop``/``clear``/``copy`` operations of ``/graph-op``
    (routes/named_rules.py) all check the identical grant, against the
    identical kind of target, for the identical reason. Same fail-closed,
    admin-bypass-only default as ``check_replace`` everywhere else:
    absence of a matching grant is a 403, never a silent allow.
    """
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")

    async def _query_fn(q: str) -> dict:
        return await client.select(conn, q)

    decision = await check_replace(
        _query_fn, conn.holons_graph, person=animus.person, target=iri
    )
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail=f"{decision.reason} (graph: {iri})"
        )

    try:
        await client.drop_graph(conn, iri)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    await after_write(client, conn, graphs=[iri], source=f"DELETE /graph <{iri}>")
    return {"ok": True, "dropped": iri, "dataset": conn.dataset}
