"""Holon retrieval, creation, SHACL validation, sequence minting, and meta routes.

CHANGED 2026-08-28: added ``POST /holon`` (create_holon) alongside the
existing ``GET /holon``. It accepts a DataBook message rather than raw
Turtle: parse the DataBook, pull its primary RDF block (``turtle``,
``turtle12``, or ``json-ld``), resolve a target graph from the request or
the DataBook's own ``graph.named_graph`` frontmatter, and hand off to
``holonbridge.ingest.write_turtle_to_graph`` -- the exact same ACL/SHACL/
write path ``POST /graph/push`` uses. See that module's docstring for why
this is a wrapper and not a second implementation.

CHANGED 2026-09-30: ``POST /validate`` understands the dataset's context
shapes graph. With no ``shapes_graph`` named it now runs the ordinary shapes
AND (if the dataset has one) the context pack, exactly as the write path does
-- so a preview and a write agree. ``merge_context`` validates a named shapes
graph against the target with the dataset's context graphs merged in, for
trying a context pack before it is installed. Which graphs are merged is
decided by the dataset (``Conn.validation_context_graphs``), never by the
request. See ``holonbridge.shacl`` for the mechanism.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .. import delegation as dg
from .. import sequence as sequence_mod
from .. import shacl as shacl_mod
from ..databook import DataBook
from ..deps import AnimusDep, BanksDep, ClientDep, ConnDep, PersonasDep, SettingsDep
from ..fuseki import FusekiError
from ..holon import PROJECTION_MODES, AsOfUnsupportedError, get_holon
from ..ingest import write_turtle_to_graph
from ..persona_scope import SCOPED_ROLES, resolve_scope_graphs
from ..jsonld import json_ld_to_turtle
from ..turtle import TurtleSyntaxError, parse

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
    on_behalf_of: str | None = Query(
        default=None,
        description=(
            "Read as this Person's scope (slug or full IRI), which requires an "
            "active read Delegation from them to you. The persona comes from the "
            "delegation, and only the graphs it names are readable."
        ),
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

    CHANGED 2026-09-29: ``on_behalf_of`` is the one deliberate, guarded
    exception to the paragraph above. It names another Person, and is
    honoured only if that Person has an active, unrevoked read Delegation
    to the caller (see ``holonbridge.delegation``). Even then the scope is
    that Person's own, restricted to exactly the private graphs the
    delegation lists -- a delegation covering ``holons`` alone does not
    expose that Person's ``scene`` graph -- and the caller's own private
    graphs are not added to it. With no active delegation the answer is a
    403, never a silent fallback to the caller's own scope.
    """
    if projection_mode not in PROJECTION_MODES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"projection_mode must be one of {', '.join(PROJECTION_MODES)}",
        )
    persona, _persona_source = personas.get(person_id=animus.person, dataset=conn.dataset)

    delegator: str | None = None
    if on_behalf_of:
        try:
            delegator = (
                on_behalf_of if ":" in on_behalf_of else conn.person_iri(on_behalf_of)
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if delegator == animus.person:
            delegator = None  # asking to read as yourself is just a normal read

    if delegator is None:
        scope = resolve_scope_graphs(conn, person_id=animus.person, persona=persona)
    else:

        async def query_fn(q: str) -> dict:
            return await client.select(conn, q)

        try:
            delegated = await dg.resolve_read_delegation(
                query_fn,
                conn.holons_graph,
                delegate=animus.person,
                delegator=delegator,
                current_persona=persona,
                persona_iri_of=conn.persona_graph,
            )
        except dg.DelegationRefused as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except dg.DelegationAmbiguous as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except dg.DelegationError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except FusekiError as exc:
            raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

        scope = resolve_scope_graphs(
            conn, person_id=delegator, persona=delegated.persona
        )
        delegator_private = {
            conn.persona_user_graph(delegated.persona, role, conn.person_slug(delegator))
            for role in SCOPED_ROLES
        }
        # Keep ground truth, the persona's public graphs, and only those of
        # the delegator's private graphs the delegation actually names.
        scope = [
            g for g in scope if g.iri not in delegator_private or g.iri in delegated.graphs
        ]
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
    except AsOfUnsupportedError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
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
            turtle_payload = await json_ld_to_turtle(block.body, settings=settings)
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
    context: bool = Field(
        default=True,
        description=(
            "When no shapes_graph is named, also run the dataset's context "
            "shapes graph (urn:{dataset}:shacl-context) against the target "
            "merged with the dataset's context graphs, exactly as the write "
            "path does. Ignored when shapes_graph is given. A dataset with no "
            "context shapes is unaffected."
        ),
    )
    merge_context: bool = Field(
        default=False,
        description=(
            "Validate shapes_graph against the target with the dataset's "
            "context graphs (its ontology) merged in, in a single run. For "
            "trying a context pack before it is installed, or any shapes "
            "graph that needs the ontology visible. The graphs merged are the "
            "dataset's own; a request cannot name others."
        ),
    )


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

    context_graphs = conn.validation_context_graphs if body.merge_context else ()

    context_report = None
    try:
        if mode == "delta":
            report = await shacl_mod.validate_delta(
                client,
                conn,
                turtle=body.turtle,
                shapes_graph=shapes_graph,
                target_graph=body.target_graph,  # type: ignore[arg-type]
                context_graphs=context_graphs,
            )
        else:
            report = await shacl_mod.validate_full(
                client,
                conn,
                turtle=body.turtle,
                shapes_graph=shapes_graph,
                target_graph=body.target_graph,
                context_graphs=context_graphs,
            )
        # Like the write path: with no shapes graph named, the dataset's own
        # context pack runs too. merge_context already folds the ontology into
        # this run, so it opts out of the separate pack.
        if body.context and body.shapes_graph is None and not body.merge_context:
            context_report = await shacl_mod.validate_context_pack(
                client,
                conn,
                turtle=body.turtle,
                target_graph=body.target_graph,
                delta=(mode == "delta"),
            )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    out = {
        "shapes_graph": shapes_graph,
        **shacl_mod.combine_reports(
            report,
            context_report,
            context_shapes_graph=conn.context_shapes_graph,
            context_graphs=conn.validation_context_graphs,
        ),
    }
    if context_graphs:
        out["context_graphs"] = list(context_graphs)
    return out


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
