"""Holon retrieval, creation, SHACL validation, sequence minting, and meta routes.

CHANGED 2026-08-28: added ``POST /holon`` (create_holon) alongside the
existing ``GET /holon``. It accepts a DataBook message rather than raw
Turtle: parse the DataBook, pull its primary RDF block (``turtle``,
``turtle12``, or ``json-ld``), resolve a target graph from the request or
the DataBook's own ``graph.named_graph`` frontmatter, and hand off to
``holonbridge.ingest.write_turtle_to_graph`` -- the exact same ACL/SHACL/
write path ``POST /graph/push`` uses. See that module's docstring for why
this is a wrapper and not a second implementation.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .. import sequence as sequence_mod
from .. import shacl as shacl_mod
from ..databook import DataBook
from ..deps import AnimusDep, BanksDep, ClientDep, ConnDep, PersonasDep, SettingsDep
from ..fuseki import FusekiError
from ..holon import PROJECTION_MODES, get_holon
from ..ingest import write_turtle_to_graph
from ..persona_scope import resolve_scope_graphs
from ..turtle import TurtleSyntaxError, from_json_ld, parse

router = APIRouter(tags=["holon"])


# --- holon --------------------------------------------------------------------


@router.get("/holon", response_class=PlainTextResponse)
async def holon(
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    personas: PersonasDep,
    iri: str = Query(..., description="holon IRI"),
    projection_mode: str = Query(default="immersive"),
    include_shapes: bool = Query(default=True),
    observed_at: datetime | None = Query(
        default=None,
        description="Valid-time as-of read over fluent history. Omit for current state. NOT persona-scoped yet -- see holon.py's module docstring.",
    ),
    inserted_at: datetime | None = Query(
        default=None,
        description="Transaction-time bound, paired with observed_at. Defaults to observed_at when only one is given.",
    ),
) -> str:
    """Return a holon as a rendered DataBook.

    CHANGED 2026-08-17: this route now requires a resolved identity
    (``AnimusDep``), which it did not before -- same shape as the
    ``/endpoint`` change from the switch_persona PR. A caller hitting
    ``/holon`` with only a bearer token and no animus header will start
    getting a 401 and needs to send ``X-Holon-Animus-Id`` like every
    other identity-gated route.

    This is what makes persona scoping real rather than advisory: the
    read scope is resolved from the caller's own credential and their own
    current persona switch (see ``persona_scope.resolve_scope_graphs``),
    never from anything the request itself supplies -- there is no
    ``persona=`` query parameter, so a caller can only ever get their own
    scope, not name someone else's or one they haven't switched into.
    With no persona switched, scope is exactly ground truth, matching
    this route's behaviour before this change.
    """
    if projection_mode not in PROJECTION_MODES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"projection_mode must be one of {', '.join(PROJECTION_MODES)}",
        )
    persona, _persona_source = personas.get(person_id=animus.person, dataset=conn.dataset)
    scope = resolve_scope_graphs(conn, person_id=animus.person, persona=persona)
    try:
        book = await get_holon(
            client,
            conn,
            holon_iri=iri,
            scope=scope,
            projection_mode=projection_mode,
            include_shapes=include_shapes,
            observed_at=observed_at,
            inserted_at=inserted_at,
        )
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return book.render()


class CreateHolonRequest(BaseModel):
    databook: str = Field(
        ...,
        min_length=1,
        description=(
            "Full DataBook markdown text: YAML frontmatter plus one or more "
            "fenced blocks, at least one of which is turtle, turtle12, or "
            "json-ld."
        ),
    )
    block_id: str | None = Field(
        default=None,
        description=(
            "Select a specific databook:id block to ingest. Defaults to the "
            "DataBook's first turtle/turtle12/json-ld block."
        ),
    )
    graph_iri: str | None = Field(
        default=None,
        description=(
            "Target named graph. Overrides the DataBook's own "
            "graph.named_graph frontmatter if both are given; one of the "
            "two must be present."
        ),
    )
    shapes_graph: str | None = None
    mode: str = Field(default="merge", pattern="^(merge|replace)$")
    reduction_rule_id: str | None = None


@router.post("/holon")
async def create_holon(
    body: CreateHolonRequest,
    conn: ConnDep,
    client: ClientDep,
    settings: SettingsDep,
    animus: AnimusDep,
) -> dict:
    """Create (or merge into) a holon from a DataBook message.

    This is ``databook push`` -- extract the primary RDF block, resolve
    its target graph, validate, write -- exposed as a bridge-native
    operation instead of something only the DataBook CLI does client-side.
    It does not mint identity: the payload's own Turtle/JSON-LD declares
    whatever subject IRIs the holon needs, the same as a hand-authored
    push. A caller that also wants a fresh IRI should mint one first via
    ``POST /sequence/mint`` and embed it in the DataBook it sends here.

    Graph resolution order: the request's own ``graph_iri`` first, then
    the DataBook's ``graph.named_graph`` frontmatter, then a 400 if
    neither is present -- deliberately no default graph, unlike
    create_message, because a holon with no declared home is very likely
    a mistake rather than an intentional choice.
    """
    try:
        book = DataBook.parse(body.databook)
    except Exception as exc:  # DataBook.parse's own YAML loader can raise a range of errors
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"could not parse DataBook: {exc}"
        ) from exc

    try:
        block = book.block(body.block_id) if body.block_id else book.primary_graph_block()
    except (KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if block.lang == "json-ld":
        try:
            turtle_payload = from_json_ld(block.body)
        except TurtleSyntaxError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    else:
        turtle_payload = block.body
        if settings.parse_mode == "local":
            try:
                parse(turtle_payload)
            except TurtleSyntaxError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    graph_iri = body.graph_iri or book.named_graph
    if not graph_iri:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=(
                "no graph_iri supplied and the DataBook has no graph.named_graph "
                "in its frontmatter; one of the two is required"
            ),
        )

    result = await write_turtle_to_graph(
        turtle=turtle_payload,
        graph_iri=graph_iri,
        mode=body.mode,
        shapes_graph=body.shapes_graph,
        reduction_rule_id=body.reduction_rule_id,
        conn=conn,
        client=client,
        shacl_required=settings.shacl_required,
        shacl_delta=settings.shacl_delta,
        animus=animus,
    )
    return {**result, "sourceBlock": block.id or "(unlabelled)", "sourceLang": block.lang}


# --- validation ---------------------------------------------------------------


class ValidateRequest(BaseModel):
    turtle: str = Field(..., min_length=1)
    shapes_graph: str | None = None
    target_graph: str | None = Field(
        default=None,
        description="validate merged with this graph; required for delta mode",
    )
    mode: str = Field(default="auto", pattern="^(auto|full|delta)$")


@router.post("/validate")
async def validate(
    body: ValidateRequest, conn: ConnDep, client: ClientDep, settings: SettingsDep
) -> dict:
    shapes_graph = body.shapes_graph or conn.shapes_graph

    mode = body.mode
    if mode == "auto":
        mode = "delta" if (settings.shacl_delta and body.target_graph) else "full"
    if mode == "delta" and not body.target_graph:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="delta mode requires target_graph"
        )

    try:
        if mode == "delta":
            report = await shacl_mod.validate_delta(
                client,
                conn,
                turtle=body.turtle,
                shapes_graph=shapes_graph,
                target_graph=body.target_graph,  # type: ignore[arg-type]
            )
        else:
            report = await shacl_mod.validate_full(
                client,
                conn,
                turtle=body.turtle,
                shapes_graph=shapes_graph,
                target_graph=body.target_graph,
            )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {"shapes_graph": shapes_graph, **report.as_dict()}


# --- sequences ----------------------------------------------------------------


class MintRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    purpose: str = Field(..., min_length=1)
    authorised_by: str | None = None
    prefix: str | None = None
    pad: int = Field(default=4, ge=1, le=12)


@router.post("/sequence/mint")
async def mint(body: MintRequest, conn: ConnDep, client: ClientDep) -> dict:
    try:
        minted = await sequence_mod.mint(
            client,
            conn,
            name=body.name,
            purpose=body.purpose,
            authorised_by=body.authorised_by,
            prefix=body.prefix,
            pad=body.pad,
        )
    except sequence_mod.SequenceError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {
        "sequence": minted.sequence,
        "value": minted.value,
        "id": minted.identifier,
        "iri": minted.iri,
        "graph": conn.graph("sequences"),
    }


@router.get("/sequence/{name}")
async def peek(name: str, conn: ConnDep, client: ClientDep) -> dict:
    try:
        value = await sequence_mod.peek(client, conn, name=name)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"sequence": name, "value": value, "graph": conn.graph("sequences")}


# --- meta ---------------------------------------------------------------------

meta_router = APIRouter(tags=["meta"])


@meta_router.get("/health")
async def health(settings: SettingsDep) -> dict:
    """Unauthenticated liveness probe. Says nothing about the backend."""
    return {"ok": True, "service": "holonbridge-py", "shacl_required": settings.shacl_required}


@meta_router.get("/endpoint")
async def get_endpoint(
    conn: ConnDep, client: ClientDep, animus: AnimusDep, personas: PersonasDep
) -> dict:
    """Show the active bank, dataset, and canonical graph IRIs, plus this
    caller's persona override for the current dataset.

    CHANGED 2026-08-17: this route now requires a resolved identity
    (AnimusDep), which it did not before -- ``personaOverride`` is
    per-person, so reporting it accurately means knowing who is asking.
    Every route that already required AnimusDep (whoami, the sparql
    endpoints) is unaffected; a caller that was hitting ``/endpoint`` with
    only a bearer token and no animus header will start getting a 401
    here and needs to start sending ``X-Holon-Animus-Id`` like every other
    identity-gated route.

    ``personaOverride`` is this person's active persona for
    ``conn.dataset`` (``null`` if none). ``personaOverrideSource`` is
    ``explicit`` (set by switch_persona this run), ``persisted``
    (restored from a prior run), ``env`` (HOLONBRIDGE_PERSONA, only when
    this person has no stored entry), or ``none``.
    """
    persona, persona_source = personas.get(person_id=animus.person, dataset=conn.dataset)
    return {
        **conn.describe(),
        "reachable": await client.ping(conn),
        "personaOverride": persona,
        "personaOverrideSource": persona_source,
    }


@meta_router.get("/endpoints")
async def list_endpoints(banks: BanksDep) -> dict:
    return {"banks": banks.list()}


class SetEndpointRequest(BaseModel):
    name: str


@meta_router.post("/endpoint")
async def set_endpoint(body: SetEndpointRequest, banks: BanksDep) -> dict:
    try:
        bank = banks.set_active(body.name)
    except KeyError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"unknown bank: {body.name}"
        ) from exc
    return {"ok": True, "active": bank.as_public()}


@meta_router.post("/endpoints/reload")
async def reload_endpoints(banks: BanksDep) -> dict:
    banks.reload()
    return {"ok": True, "banks": banks.list()}
