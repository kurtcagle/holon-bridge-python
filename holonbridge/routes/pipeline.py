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
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from ..conn import Conn
from ..databook import DataBook
from ..deps import ClientDep, ConnDep, SettingsDep
from ..fetch import SourceFetchError, fetch_source
from ..fuseki import FusekiClient, FusekiError
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
from ..shacl import validate_delta, validate_full
from ..turtle import literal

log = logging.getLogger("holonbridge.routes.pipeline")

router = APIRouter(tags=["pipeline"])


def _spawn(request: Request, coro) -> None:  # noqa: ANN001
    """Run a coroutine in the background, holding a strong reference to it."""
    tasks: set[asyncio.Task] = request.app.state.tasks
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)


# --- registration -------------------------------------------------------------


class RegisterPipelineRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    manifest: str = Field(..., min_length=1, description="manifest Turtle")
    label: str | None = None
    replace: bool = True


@router.post("/pipeline")
async def register_pipeline(
    body: RegisterPipelineRequest, conn: ConnDep, client: ClientDep
) -> dict:
    """Register a manifest into its own graph and index it."""
    graph = conn.scoped("pipelines", body.id)
    index = conn.graph("pipelines")

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
async def get_pipelines(conn: ConnDep, client: ClientDep) -> dict:
    pipelines = await list_pipelines(client, conn)
    return {"dataset": conn.dataset, "count": len(pipelines), "pipelines": pipelines}


@router.get("/pipeline/{pipeline_id}")
async def get_pipeline(pipeline_id: str, conn: ConnDep, client: ClientDep) -> dict:
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
async def drop_pipeline(pipeline_id: str, conn: ConnDep, client: ClientDep) -> dict:
    graph = conn.scoped("pipelines", pipeline_id)
    index = conn.graph("pipelines")
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
    body: RunPipelineRequest, request: Request, conn: ConnDep, client: ClientDep
) -> dict:
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
) -> dict:
    """Accept a payload, validate it, land it, and optionally run a pipeline.

    Four shapes of inbound signal: an inline `turtle` payload, an inline
    `databook` document, a `source_graph` already in the store, or a
    `source_url` to fetch. All four land in `graph_iri` under the same
    validation gate as `/graph/push`, so ingestion cannot become a way
    around the SHACL gate.

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

        shapes = body.shapes_graph or (conn.shapes_graph if settings.shacl_required else None)
        if shapes:
            report = (
                await validate_delta(
                    client,
                    conn,
                    turtle=turtle,
                    shapes_graph=shapes,
                    target_graph=graph_iri,
                    write_mode=body.mode,
                    reduction_rule_id=body.reduction_rule_id,
                )
                if settings.shacl_delta
                else await validate_full(
                    client,
                    conn,
                    turtle=turtle,
                    shapes_graph=shapes,
                    target_graph=graph_iri,
                    write_mode=body.mode,
                    reduction_rule_id=body.reduction_rule_id,
                )
            )
            if not report.conforms:
                landing.status = "Failed"
                landing.detail = f"{len(report.results)} new violation(s) against <{shapes}>"
                mark_completed(message, failed=True, error=landing.detail)
                await store.save(conn, message)
                raise HTTPException(
                    422,
                    detail={
                        "error": "shacl_violation",
                        "messageId": message.id,
                        **report.as_dict(),
                    },
                )

        if body.mode == "replace":
            await client.put_graph(conn, graph_iri, turtle)
        else:
            await client.post_graph(conn, graph_iri, turtle)

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
