"""Ingest, pipeline, and message routes.

Runs are asynchronous by default: the caller gets a message id immediately and
polls `/message/{id}`. Two things make that safe here.

`Conn` is a frozen dataclass, so capturing it for a background task is sound
by construction — there is no request object to go stale after the response
has been sent.

Background tasks are held in a set on app state. `asyncio` keeps only a weak
reference to a running task, so a task nobody holds can be collected
mid-flight; the run would simply stop, leaving a message stuck in `Running`
with no error to explain it.

CHANGED 2026-08-29 (first pass): `/ingest` now depends on `AnimusDep` and
lands its payload through `holonbridge.ingest.write_turtle_to_graph` —
the same gated path `push_turtle`, `create_holon`, and `create_message`
already use — instead of its own inline validate-then-write logic. It had
no ACL check of any kind before this, and did its own fourth independent
copy of the validate-then-write pattern; both problems are fixed together
since the fix for one is routing through the function that already solves
the other.

CHANGED 2026-08-29 (second pass): `register_pipeline`/`drop_pipeline` now
require `AnimusDep` and check `check_write`/`check_replace` on the
pipeline's own manifest graph, the same write gate used everywhere else.
`get_pipelines`/`get_pipeline`/`pipeline_run` now require `AnimusDep` and
`PersonasDep`, gated by Toolset-reachability via a shared
`_load_and_authorise_pipeline` helper — the same pattern PR #9 already
established for named-queries/named-rules, including the "same 404 shape
for unknown-vs-unreachable" principle. List and get are gated alongside
run, not just run — a boundary that hides a pipeline from the list but
still runs it on request isn't a boundary, same reasoning routes/
named_queries.py's own docstring already states.

STILL OPEN, found while making this fix, more serious than the entry-gate
gap it resembles: a pipeline's *internal* rule stages bypass
Toolset-reachability entirely, and this pass does not touch it.
`run_pipeline()` -> `_run_rule_stage()` in `holonbridge/pipeline.py` calls
`execute_named_rule()` directly against a rule pulled from the raw
registry (`load_named_rules`), never going through
`routes/named_rules.py`'s `_load_and_authorise` gate. Gating
`pipeline_run`'s own entry point (this pass) stops an unreachable
*pipeline* from being triggered at all, but does not stop a *reachable*
pipeline from invoking a Toolset-*restricted* rule internally — the
pipeline is a full bypass of rule-level reachability for anyone who can
reach the pipeline itself. `_run_projection_stage` has the same shape of
gap for projection hooks. Fixing this properly means threading persona
identity down through `run_pipeline`/`_run_rule_stage`/
`_run_projection_stage` — a real change to holonbridge/pipeline.py's core
execution functions, not a route tweak, and deliberately not attempted
here without separate sign-off given how many functions it touches.

Also found, unrelated to pipelines specifically: `/graph-op`
(`routes/named_rules.py`) — CLEAR/DROP/CREATE/COPY/MOVE/ADD on arbitrary
named graphs — had zero `AnimusDep`, zero ACL check of any kind, and was
the most severe thing found in this whole pass (destructive,
arbitrary-graph, no auth at all) even though it sat outside the
ingest/pipeline surface this work was scoped to. Fixed 2026-08-31 — see
that route's own docstring in `routes/named_rules.py`. `DELETE /graph`
(`routes/graphs.py`) remains ungated; not touched by that fix.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from ..acl import check_replace, check_write
from ..conn import Conn
from ..databook import DataBook
from ..deps import AnimusDep, ClientDep, ConnDep, PersonasDep, SettingsDep
from ..fetch import SourceFetchError, fetch_source
from ..fuseki import FusekiClient, FusekiError
from ..ingest import write_turtle_to_graph
from ..messages import (
    Message,
    MessageStore,
    StageRecord,
    mark_completed,
    mark_running,
)
from ..pipeline import (
    BUILD,
    PipelineError,
    Manifest,
    load_manifest,
    list_pipelines,
    run_pipeline,
    topological_order,
)
from ..toolset import resolve_reachable
from ..turtle import literal

log = logging.getLogger("holonbridge.routes.pipeline")

router = APIRouter(tags=["pipeline"])


def _spawn(request: Request, coro) -> None:  # noqa: ANN001
    """Run a coroutine in the background, holding a strong reference to it."""
    tasks: set[asyncio.Task] = request.app.state.tasks
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def _stringify_detail(detail: Any) -> str:
    """Render an HTTPException.detail (str or dict) as plain text for a
    message stage's own `detail` field, which is a string everywhere else
    in this module. `write_turtle_to_graph` raises with a dict detail for
    the 422/502 cases (SHACL violation, Fuseki error) and a plain string
    for 401/403/400 -- this normalises either into one line rather than
    dumping the raw dict into a field meant for a short human-readable
    summary.
    """
    if isinstance(detail, dict):
        if detail.get("error") == "shacl_violation":
            return f"{detail.get('violations', '?')} new violation(s) against the shapes graph"
        return str(detail.get("message") or detail.get("error") or detail)
    return str(detail)


def _not_found_pipeline(pipeline_id: str, available_ids: list[str]) -> HTTPException:
    """Same 404 shape whether the id is genuinely unknown or just outside
    this caller's reachable set -- see routes/named_queries.py's matching
    helper; a restricted pipeline shouldn't differentially reveal its own
    existence either."""
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={
            "error": "unknown_pipeline",
            "id": pipeline_id,
            "available": available_ids,
        },
    )


async def _reachable_pipeline_ids(
    entries: list[dict], conn, client, persona: str | None
) -> set[str]:
    """The subset of `entries` (from list_pipelines, by id) this persona
    can reach. Shared by list/get/run so all three agree on exactly the
    same set -- same shape as routes/named_queries.py's helper of the
    same purpose, adapted for list_pipelines' plain dicts rather than a
    NamedQuery dataclass."""

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    id_by_iri = {e["graph"]: e["id"] for e in entries}
    reachable_iris = await resolve_reachable(
        query_fn, conn, persona=persona, candidate_iris=list(id_by_iri)
    )
    return {id_by_iri[iri] for iri in reachable_iris}


async def _load_and_authorise_pipeline(
    pipeline_id: str, conn, client, animus, personas
) -> tuple[dict, str | None]:
    """Look up one pipeline's index entry and confirm this caller's persona
    can reach it -- otherwise raise the shared 404. Centralised so
    list/get/run can't drift on what "reachable" means, same shape as
    routes/named_queries.py's helper of the same name."""
    entries = await list_pipelines(client, conn)
    entry = next((e for e in entries if e["id"] == pipeline_id), None)
    available_ids = [e["id"] for e in entries]
    if entry is None:
        raise _not_found_pipeline(pipeline_id, available_ids)

    persona, _source = personas.get(person_id=animus.person, dataset=conn.dataset)
    reachable_ids = await _reachable_pipeline_ids(entries, conn, client, persona)

    if pipeline_id not in reachable_ids:
        raise _not_found_pipeline(pipeline_id, sorted(reachable_ids))

    return entry, persona


# --- registration -------------------------------------------------------------


class RegisterPipelineRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    manifest: str = Field(..., min_length=1, description="manifest Turtle")
    label: str | None = None
    replace: bool = True


@router.post("/pipeline")
async def register_pipeline(
    body: RegisterPipelineRequest, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    """Register a manifest into its own graph and index it.

    Gated the same way any other RDF write is: `replace=True` (the
    default — registration always fully replaces or creates the manifest
    graph, never appends into an existing one) requires `check_replace`;
    `replace=False` requires `check_write`. Gated on the pipeline's own
    manifest graph, not the shared pipelines index — the index write is
    bookkeeping intrinsic to a successful registration, not a
    separately-requested write needing its own grant.
    """
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")

    graph = conn.scoped("pipelines", body.id)
    index = conn.graph("pipelines")

    async def _query_fn(q: str) -> dict:
        return await client.select(conn, q)

    if body.replace:
        decision = await check_replace(
            _query_fn, conn.holons_graph, person=animus.person, target=graph
        )
    else:
        decision = await check_write(
            _query_fn, conn.holons_graph, person=animus.person, target=graph
        )
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail=f"{decision.reason} (graph: {graph})"
        )

    try:
        if body.replace:
            await client.put_graph(conn, graph, body.manifest)
        else:
            await client.post_graph(conn, graph, body.manifest)

        registered = datetime.now(timezone.utc).isoformat(timespec="seconds")
        await client.update(
            conn,
            f"""DELETE {{ GRAPH <{index}> {{ <{graph}> ?p ?o }} }}
WHERE {{ GRAPH <{index}> {{ <{graph}> ?p ?o }} }}""",
        )
        await client.update(
            conn,
            f"""INSERT DATA {{
  GRAPH <{index}> {{
    <{graph}> a <{BUILD}Manifest> ;
      <{BUILD}pipelineId> {literal(body.id)} ;
      <{BUILD}graph> {literal(graph)} ;
      <http://www.w3.org/2000/01/rdf-schema#label> {literal(body.label or body.id)} ;
      <{BUILD}registeredAt> {literal(registered, datatype="<http://www.w3.org/2001/XMLSchema#dateTime>")} .
  }}
}}""",
        )
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    manifest = await load_manifest(client, conn, body.id)
    try:
        order = topological_order(manifest)
    except PipelineError as exc:
        # Registered but unrunnable. Say so now rather than at first run.
        return {
            "ok": True,
            "id": body.id,
            "graph": graph,
            "runnable": False,
            "error": str(exc),
        }

    return {"ok": True, "id": body.id, "graph": graph, "runnable": True, **manifest.summary(order)}


@router.get("/pipelines")
async def get_pipelines(
    conn: ConnDep, client: ClientDep, animus: AnimusDep, personas: PersonasDep
) -> dict:
    entries = await list_pipelines(client, conn)
    persona, _source = personas.get(person_id=animus.person, dataset=conn.dataset)
    reachable_ids = await _reachable_pipeline_ids(entries, conn, client, persona)

    visible = [e for e in entries if e["id"] in reachable_ids]
    return {"dataset": conn.dataset, "count": len(visible), "pipelines": visible}


@router.get("/pipeline/{pipeline_id}")
async def get_pipeline(
    pipeline_id: str,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    personas: PersonasDep,
) -> dict:
    await _load_and_authorise_pipeline(pipeline_id, conn, client, animus, personas)
    manifest = await load_manifest(client, conn, pipeline_id)
    if not manifest.nodes:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_pipeline", "id": pipeline_id, "graph": manifest.graph},
        )
    try:
        order = topological_order(manifest)
    except PipelineError as exc:
        return {**manifest.summary(), "runnable": False, "error": str(exc)}
    return {**manifest.summary(order), "runnable": True}


@router.delete("/pipeline/{pipeline_id}")
async def drop_pipeline(
    pipeline_id: str, conn: ConnDep, client: ClientDep, animus: AnimusDep
) -> dict:
    """Gated by `check_replace` — dropping an existing pipeline is a
    destructive, wholesale operation on already-registered content, not
    an append; same reasoning that makes `grantsReplace` the harder-to-get
    grant everywhere else in this codebase.
    """
    if animus.person is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="unresolved identity")

    graph = conn.scoped("pipelines", pipeline_id)
    index = conn.graph("pipelines")

    async def _query_fn(q: str) -> dict:
        return await client.select(conn, q)

    decision = await check_replace(
        _query_fn, conn.holons_graph, person=animus.person, target=graph
    )
    if not decision.allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail=f"{decision.reason} (graph: {graph})"
        )

    try:
        await client.drop_graph(conn, graph)
        await client.update(
            conn,
            f"""DELETE {{ GRAPH <{index}> {{ <{graph}> ?p ?o }} }}
WHERE {{ GRAPH <{index}> {{ <{graph}> ?p ?o }} }}""",
        )
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc
    return {"ok": True, "id": pipeline_id, "graph": graph}


# --- running ------------------------------------------------------------------


class RunPipelineRequest(BaseModel):
    pipeline: str = Field(..., min_length=1)
    params: dict[str, object] = Field(default_factory=dict)
    stop_on_error: bool = True
    wait: bool = Field(
        default=False, description="run inline and return the finished message"
    )


@router.post("/pipeline-run")
async def pipeline_run(
    body: RunPipelineRequest,
    request: Request,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    personas: PersonasDep,
) -> dict:
    """Gates the entry point: whether this caller's persona can reach the
    named *pipeline* at all, same shape as `run_named_rule`. Does NOT gate
    what the pipeline does internally -- see this module's docstring for
    why that's a separate, larger, not-yet-attempted fix.
    """
    await _load_and_authorise_pipeline(body.pipeline, conn, client, animus, personas)

    manifest = await load_manifest(client, conn, body.pipeline)
    if not manifest.nodes:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_pipeline", "id": body.pipeline},
        )
    try:
        topological_order(manifest)
    except PipelineError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "unrunnable_manifest", "message": str(exc)},
        ) from exc

    store = MessageStore(client)
    message = Message(id=Message.new_id(), pipeline=body.pipeline)
    await store.save(conn, message)

    if body.wait:
        await _execute(conn, client, manifest, message, body.params, body.stop_on_error)
        return message.as_dict()

    _spawn(
        request,
        _execute(conn, client, manifest, message, body.params, body.stop_on_error),
    )
    return {"messageId": message.id, "status": message.status, "pipeline": body.pipeline}


async def _execute(
    conn: Conn,
    client: FusekiClient,
    manifest: Manifest,
    message: Message,
    params: dict,
    stop_on_error: bool,
) -> None:
    store = MessageStore(client)
    mark_running(message)
    await store.save(conn, message)

    try:
        await run_pipeline(
            conn, client, manifest, message, params=params, stop_on_error=stop_on_error
        )
        failed = any(s.status == "Failed" for s in message.stages)
        mark_completed(message, failed=failed, error=message.error)
    except Exception as exc:  # noqa: BLE001 - the message is the error channel
        log.exception("pipeline %s failed", manifest.id)
        mark_completed(message, failed=True, error=str(exc))

    try:
        await store.save(conn, message)
    except FusekiError:
        log.exception("could not record the outcome of message %s", message.id)


# --- ingest -------------------------------------------------------------------


def _looks_like_databook(content: str, content_type: str) -> bool:
    """Heuristic only: a DataBook starts with YAML frontmatter, and a server
    serving one is likely to say so in the content-type. Either signal is
    enough — requiring both would miss a DataBook served as text/plain.
    """
    return content.lstrip().startswith("---") or "markdown" in content_type.lower()


def _extract_databook(text: str, graph_iri: str | None) -> tuple[str, str | None]:
    """Parse a DataBook and return its primary RDF block plus a target graph.

    An explicit ``graph_iri`` always wins over what the DataBook's own
    frontmatter declares — the caller's instruction is more specific than
    whatever default the document happened to be authored with.
    """
    db = DataBook.parse(text)
    turtle = db.primary_graph_block().body
    return turtle, graph_iri or db.named_graph


class IngestRequest(BaseModel):
    turtle: str | None = Field(default=None, description="inline turtle payload")
    databook: str | None = Field(
        default=None, description="inline DataBook document (Markdown)"
    )
    source_graph: str | None = Field(
        default=None, description="ingest a graph already in the store"
    )
    source_url: str | None = Field(
        default=None,
        description=(
            "fetch a remote source: a DataBook (frontmatter present) or "
            "raw Turtle, detected from what is fetched"
        ),
    )
    graph_iri: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "target named graph; required unless a databook or source_url "
            "supplies graph.named_graph in its frontmatter"
        ),
    )
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
    pipeline: str | None = Field(
        default=None, description="pipeline to run once the payload has landed"
    )
    params: dict[str, object] = Field(default_factory=dict)
    wait: bool = False


@router.post("/ingest")
async def ingest(
    body: IngestRequest,
    request: Request,
    conn: ConnDep,
    client: ClientDep,
    settings: SettingsDep,
    animus: AnimusDep,
) -> dict:
    """Accept a payload, validate it, land it, and optionally run a pipeline.

    Four shapes of inbound signal: an inline `turtle` payload, an inline
    `databook` document, a `source_graph` already in the store, or a
    `source_url` to fetch. All four land in `graph_iri` through
    `holonbridge.ingest.write_turtle_to_graph` — the same ACL-checked,
    SHACL-gated write path `/graph/push`, `create_holon`, and
    `create_message` use — so ingestion cannot become a way around either
    gate. `mode="replace"` requires `check_replace`; `mode="merge"`
    (the default) requires `check_write`, same as everywhere else that
    calls this function.

    A `databook` or `source_url` source may supply its own target graph via
    `graph.named_graph` in its frontmatter, so `graph_iri` is optional for
    those two shapes — a DataBook that already declares where it belongs
    shouldn't require the caller to repeat that. An explicit `graph_iri`
    always wins over what the DataBook declares.
    """
    sources = (body.turtle, body.databook, body.source_graph, body.source_url)
    if sum(bool(s) for s in sources) != 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="supply exactly one of turtle, databook, source_graph, or source_url",
        )

    store = MessageStore(client)
    message = Message(
        id=Message.new_id(),
        pipeline=body.pipeline or "",
        target_graph=body.graph_iri or "",
    )
    mark_running(message)
    await store.save(conn, message)

    landing = StageRecord(name="ingest", transformer="push", order=0)
    message.stages.append(landing)

    graph_iri = body.graph_iri

    try:
        turtle = body.turtle

        if body.source_graph:
            turtle = await client.get_graph(conn, body.source_graph)
            if not turtle.strip():
                raise PipelineError(f"<{body.source_graph}> is empty or absent")

        elif body.databook:
            turtle, graph_iri = _extract_databook(body.databook, graph_iri)

        elif body.source_url:
            fetch_stage = StageRecord(name="fetch", transformer="source_url", order=0)
            message.stages.append(fetch_stage)
            landing.order = 1
            try:
                content, content_type = await fetch_source(body.source_url)
            except SourceFetchError as exc:
                fetch_stage.status = "Failed"
                fetch_stage.detail = str(exc)
                raise
            fetch_stage.status = "Completed"
            fetch_stage.detail = f"{len(content)} bytes, {content_type or 'no content-type'}"

            if _looks_like_databook(content, content_type):
                turtle, graph_iri = _extract_databook(content, graph_iri)
            else:
                turtle = content

        if not graph_iri:
            raise ValueError(
                "graph_iri is required unless a databook or source_url "
                "supplies graph.named_graph"
            )
        message.target_graph = graph_iri

        try:
            await write_turtle_to_graph(
                turtle=turtle,
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
        except HTTPException as exc:
            landing.status = "Failed"
            landing.detail = _stringify_detail(exc.detail)
            mark_completed(message, failed=True, error=landing.detail)
            await store.save(conn, message)
            detail = exc.detail
            if isinstance(detail, dict):
                detail = {**detail, "messageId": message.id}
            else:
                detail = {
                    "error": "ingest_failed",
                    "messageId": message.id,
                    "message": str(detail),
                }
            raise HTTPException(exc.status_code, detail=detail) from exc

        landing.status = "Completed"
        landing.detail = f"{body.mode} into <{graph_iri}>"

    except HTTPException:
        raise
    except (PipelineError, FusekiError, SourceFetchError, ValueError) as exc:
        landing.status = "Failed"
        landing.detail = str(exc)
        mark_completed(message, failed=True, error=str(exc))
        await store.save(conn, message)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "ingest_failed", "messageId": message.id, "message": str(exc)},
        ) from exc

    if not body.pipeline:
        mark_completed(message)
        await store.save(conn, message)
        return message.as_dict()

    manifest = await load_manifest(client, conn, body.pipeline)
    if not manifest.nodes:
        mark_completed(message, failed=True, error=f"unknown pipeline {body.pipeline!r}")
        await store.save(conn, message)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_pipeline", "id": body.pipeline, "messageId": message.id},
        )

    await store.save(conn, message)

    if body.wait:
        await _execute(conn, client, manifest, message, body.params, True)
        return message.as_dict()

    _spawn(request, _execute(conn, client, manifest, message, body.params, True))
    return {"messageId": message.id, "status": message.status, "pipeline": body.pipeline}


# --- messages -----------------------------------------------------------------


@router.get("/message/{message_id}")
async def get_message(message_id: str, conn: ConnDep, client: ClientDep) -> dict:
    message = await MessageStore(client).get(conn, message_id)
    if message is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_message", "messageId": message_id},
        )
    return message.as_dict()


@router.get("/messages")
async def get_messages(
    conn: ConnDep, client: ClientDep, limit: int = Query(default=20, ge=1, le=200)
) -> dict:
    messages = await MessageStore(client).recent(conn, limit)
    return {"dataset": conn.dataset, "count": len(messages), "messages": messages}
