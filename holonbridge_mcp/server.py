"""holonbridge-mcp (Python) — stdio MCP server over the HolonBridge REST API.

This layer holds no backend logic. It calls the bridge exactly as any other
HTTP client would, which keeps one authorisation path and one validation
path rather than two that drift.

Run::

    python -m holonbridge_mcp.server        # stdio, for Claude Desktop
    python -m holonbridge_mcp --help        # full CLI, including remote transports

Environment:
    HOLONBRIDGE_URL      default http://localhost:3031
    BEARER_TOKEN         bearer token for the bridge
    HOLONBRIDGE_DATASET  optional X-Dataset-Override applied to every call
    HOLONBRIDGE_BANK     optional bank (named backend connection) for every call
    ANTHROPIC_API_KEY    required for nl_query
    ANTHROPIC_MODEL      default claude-sonnet-4-6

Read from a shared ``.env`` — see :mod:`holonbridge.envfile` — before any of
the constants below are resolved, so this module is the one place a direct
``python -m holonbridge_mcp.server`` invocation is still covered even though
it never goes through ``__main__.py``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from holonbridge.envfile import load_shared_env

log = logging.getLogger("holonbridge_mcp.server")

load_shared_env()

BRIDGE_URL = os.getenv("HOLONBRIDGE_URL", "http://localhost:3031").rstrip("/")
BEARER = os.getenv("BEARER_TOKEN", "")
# Mutable: switch_dataset changes this for the life of the process. Module
# state rather than a per-call argument because switching is a session
# gesture ("now I am working on bridgerton"), not something to repeat on
# all 49 tools. The cost is that it is genuinely global — a second client
# against the same process sees the switch too. Fine for one operator and
# their own tunnel, which is what this is; not fine for shared hosting.
#
# Persisted, not just held in memory. The failure this fixes happened live,
# twice: a chosen dataset survives fine within a session, but an MCP process
# restart used to silently fall back to whatever HOLONBRIDGE_DATASET (or
# nothing) said at import time — which meant a write immediately after a
# restart landed in the wrong dataset with no error and no warning, because
# nothing was watching for that specific kind of drift. A real env var still
# wins over the persisted file, matching how .env itself behaves — an
# explicit HOLONBRIDGE_DATASET is a deliberate pin, not a stale leftover.
_DATASET_STATE_FILE = Path(
    os.getenv("HOLONBRIDGE_DATASET_STATE_FILE", "")
    or (Path.home() / ".holonbridge" / "mcp-dataset-override")
)


def _load_persisted_dataset() -> str:
    try:
        return _DATASET_STATE_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _persist_dataset(name: str) -> None:
    """Save (or, for an empty name, clear) the dataset override to disk.

    Best-effort: a failure to persist should not stop the switch itself from
    taking effect for the rest of this session, only from surviving the next
    restart. Logged, not raised.
    """
    try:
        if name:
            _DATASET_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _DATASET_STATE_FILE.write_text(name, encoding="utf-8")
        else:
            _DATASET_STATE_FILE.unlink(missing_ok=True)
    except OSError as exc:
        log.warning(
            "could not persist dataset override to %s: %s "
            "(the switch is still in effect for this session, but will not "
            "survive a restart)",
            _DATASET_STATE_FILE,
            exc,
        )


def _resolve_initial_dataset() -> tuple[str, str]:
    """Returns (dataset, source) where source is 'env', 'persisted', or 'none'."""
    explicit = os.getenv("HOLONBRIDGE_DATASET", "").strip()
    if explicit:
        return explicit, "env"
    persisted = _load_persisted_dataset()
    if persisted:
        log.info(
            "restored dataset override %r from %s (surviving a prior "
            "restart) — set HOLONBRIDGE_DATASET explicitly to override this",
            persisted,
            _DATASET_STATE_FILE,
        )
        return persisted, "persisted"
    return "", "none"


_dataset_override, _dataset_override_source = _resolve_initial_dataset()

# The same treatment for banks. A *bank* is a named backend connection -- a
# server URL plus a default dataset -- previously called a "profile". The two
# overrides are deliberately independent: a bank selects *which store*, a
# dataset selects *which graph set within it*, and switching one should not
# silently reset the other.
#
# Kept as a parallel implementation rather than folded into one generic
# helper with the dataset functions above. The duplication is about fifteen
# lines; the alternative rewrites code that a live restart test and eight
# unit tests currently cover, to save less than it risks.
_BANK_STATE_FILE = Path(
    os.getenv("HOLONBRIDGE_BANK_STATE_FILE", "")
    or (Path.home() / ".holonbridge" / "mcp-bank-override")
)


def _load_persisted_bank() -> str:
    try:
        return _BANK_STATE_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _persist_bank(name: str) -> None:
    """Save (or, for an empty name, clear) the bank override to disk.

    Best-effort, exactly as for the dataset override: failing to persist must
    not stop the switch taking effect for this session, only from surviving a
    restart.
    """
    try:
        if name:
            _BANK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _BANK_STATE_FILE.write_text(name, encoding="utf-8")
        else:
            _BANK_STATE_FILE.unlink(missing_ok=True)
    except OSError as exc:
        log.warning(
            "could not persist bank override to %s: %s "
            "(the switch is still in effect for this session, but will not "
            "survive a restart)",
            _BANK_STATE_FILE,
            exc,
        )


def _resolve_initial_bank() -> tuple[str, str]:
    """Returns (bank, source) where source is 'env', 'persisted', or 'none'."""
    explicit = os.getenv("HOLONBRIDGE_BANK", "").strip()
    if explicit:
        return explicit, "env"
    persisted = _load_persisted_bank()
    if persisted:
        log.info(
            "restored bank override %r from %s (surviving a prior restart) "
            "— set HOLONBRIDGE_BANK explicitly to override this",
            persisted,
            _BANK_STATE_FILE,
        )
        return persisted, "persisted"
    return "", "none"


_bank_override, _bank_override_source = _resolve_initial_bank()
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")


def _transport_security():
    """Allow the tunnel's own hostname past the SDK's DNS-rebinding check.

    The MCP SDK enables DNS-rebinding protection by default with an empty
    ``allowed_hosts``, which means it accepts only localhost. That is the
    right default for a server bound to 127.0.0.1 and reached directly, and
    exactly wrong behind a tunnel: ngrok forwards with the public hostname in
    ``Host``, so every request fails the check with a 421 and a
    ``ValueError: Request validation failed`` raised from deep inside the SSE
    handler — after OAuth has already succeeded, which makes it look like an
    auth problem when it is not one.

    The public hostname is already known: it is ``MCP_PUBLIC_URL``, which the
    OAuth layer requires anyway. Deriving the allowlist from it means one less
    thing to configure and one less thing to get inconsistent.
    ``MCP_ALLOWED_HOSTS`` is there for the cases this cannot infer - a second
    tunnel, a reverse proxy in front, a custom domain.
    """
    from mcp.server.transport_security import (  # noqa: PLC0415
        TransportSecuritySettings,
    )

    hosts: list[str] = ["localhost", "127.0.0.1", "localhost:*", "127.0.0.1:*"]
    origins: list[str] = []

    public_url = os.getenv("MCP_PUBLIC_URL", "").strip()
    if public_url:
        from urllib.parse import urlparse  # noqa: PLC0415

        parsed = urlparse(public_url)
        if parsed.hostname:
            hosts.append(parsed.hostname)
            # A tunnel terminates TLS on the public side and forwards plain
            # HTTP, so Host carries no port; include both shapes rather than
            # guess which one arrives.
            if parsed.port:
                hosts.append(f"{parsed.hostname}:{parsed.port}")
        if parsed.scheme and parsed.hostname:
            origins.append(f"{parsed.scheme}://{parsed.netloc}")

    extra = os.getenv("MCP_ALLOWED_HOSTS", "").strip()
    if extra:
        hosts.extend(h.strip() for h in extra.split(",") if h.strip())

    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


mcp = FastMCP("holonbridge", transport_security=_transport_security())


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if BEARER:
        headers["Authorization"] = f"Bearer {BEARER}"
    if _dataset_override:
        headers["X-Dataset-Override"] = _dataset_override
    return headers


def _with_bank(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Apply the bank override as ``?bank=`` without discarding caller params.

    The bridge resolves the bank from the query string rather than a header
    (``deps.resolve_conn`` reads ``request.query_params["bank"]``), so this
    has to merge rather than replace -- a caller-supplied ``bank`` still wins,
    which keeps a deliberate per-call choice above the session default.
    """
    if not _bank_override:
        return params
    merged = dict(params or {})
    merged.setdefault("bank", _bank_override)
    return merged


async def _call(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    text: bool = False,
) -> Any:
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.request(
            method,
            f"{BRIDGE_URL}{path}",
            json=json_body,
            params=_with_bank(params),
            headers=_headers(),
        )
    if response.status_code >= 400:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text.strip()
        return {"error": True, "status": response.status_code, "detail": detail}
    if text:
        return response.text
    try:
        return response.json()
    except ValueError:
        return response.text


# --- P1 core ------------------------------------------------------------------


@mcp.tool()
async def get_endpoint() -> dict:
    """Show the active bank, dataset, and canonical graph IRIs.

    Also reports the MCP layer's own dataset override state — deliberately
    prominent, because a wrong-dataset write with no warning is the specific
    failure this has caused before. ``datasetOverride`` is the switch this
    MCP process currently applies (empty if none); ``datasetOverrideSource``
    is ``env`` (from ``HOLONBRIDGE_DATASET``), ``persisted`` (restored from a
    prior session after a restart), ``explicit`` (set by ``switch_dataset``
    in this session), or ``none``. Worth checking with this tool before a
    write, and especially right after any restart — a persisted override
    means the switch survived, but it is still worth confirming rather than
    assuming.

    ``bankOverride`` and ``bankOverrideSource`` report the same thing for the
    bank — the named backend connection this process is pointed at. Both are
    reported because they fail the same silent way: the call succeeds, the
    data is simply not where you thought it was.
    """
    result = await _call("GET", "/endpoint")
    if isinstance(result, dict) and not result.get("error"):
        result["datasetOverride"] = _dataset_override
        result["datasetOverrideSource"] = _dataset_override_source
        result["bankOverride"] = _bank_override
        result["bankOverrideSource"] = _bank_override_source
    return result


@mcp.tool()
async def list_endpoints() -> dict:
    """List all named banks and which one is active, as the bridge sees it."""
    return await _call("GET", "/endpoints")


@mcp.tool()
async def set_endpoint(name: str) -> dict:
    """Switch the bridge's own active bank by name (server-side, affects every client)."""
    return await _call("POST", "/endpoint", json_body={"name": name})


@mcp.tool()
async def sparql_select(query: str, graph: str | None = None) -> dict:
    """Run a SPARQL SELECT or ASK. Returns SPARQL JSON results."""
    return await _call(
        "POST", "/sparql/select", json_body={"query": query, "graph": graph}
    )


@mcp.tool()
async def sparql_construct(query: str, graph: str | None = None) -> str:
    """Run a SPARQL CONSTRUCT or DESCRIBE. Returns Turtle."""
    return await _call(
        "POST", "/sparql/construct", json_body={"query": query, "graph": graph}, text=True
    )


@mcp.tool()
async def sparql_update(update: str) -> dict:
    """Run a SPARQL UPDATE (INSERT DATA, DELETE, CLEAR, DROP, COPY)."""
    return await _call("POST", "/sparql/update", json_body={"update": update})


@mcp.tool()
async def push_turtle(
    turtle: str,
    graph_iri: str,
    shapes_graph: str | None = None,
    mode: str = "merge",
    reduction_rule_id: str | None = None,
) -> dict:
    """Push Turtle into a named graph.

    ``mode`` is ``merge`` (GSP POST) or ``replace`` (GSP PUT). Supplying
    ``shapes_graph`` validates before the write and rejects on new violations.

    ``reduction_rule_id`` names a registered named rule that reduces the
    candidate write to its current state before validating — mechanism for
    fluent-style data, where a shape's cardinality constraints only make
    sense against "what's current," not the full history a bitemporal graph
    accumulates. The rule itself defines what "current" means.
    """
    return await _call(
        "POST",
        "/graph/push",
        json_body={
            "turtle": turtle,
            "graph_iri": graph_iri,
            "shapes_graph": shapes_graph,
            "mode": mode,
            "reduction_rule_id": reduction_rule_id,
        },
    )


@mcp.tool()
async def get_holon(holon_iri: str, projection_mode: str = "immersive") -> str:
    """Retrieve a holon as a DataBook.

    Projection modes: immersive, cinematic, active_inference, exploded_view.
    """
    return await _call(
        "GET",
        "/holon",
        params={"iri": holon_iri, "projection_mode": projection_mode},
        text=True,
    )


@mcp.tool()
async def list_graphs(filter: str | None = None) -> dict:
    """List named graphs with triple counts, optionally filtered by substring."""
    return await _call("GET", "/graphs", params={"filter": filter} if filter else None)


# --- P2 SHACL -----------------------------------------------------------------


@mcp.tool()
async def validate_turtle(
    turtle: str,
    shapes_graph: str | None = None,
    target_graph: str | None = None,
    mode: str = "auto",
) -> dict:
    """Validate Turtle against a shapes graph already in the store.

    Passing ``target_graph`` validates the payload merged with that graph and
    reports only newly introduced violations, which is what the write path
    does. Without it, the payload is validated in isolation — useful, but it
    will flag cross-references the target graph would have satisfied.
    """
    return await _call(
        "POST",
        "/validate",
        json_body={
            "turtle": turtle,
            "shapes_graph": shapes_graph,
            "target_graph": target_graph,
            "mode": mode,
        },
    )


# --- P3 NL --------------------------------------------------------------------


@mcp.tool()
async def nl_query(question: str, graph: str | None = None) -> dict:
    """Answer a natural-language question by generating and running SPARQL.

    Returns the generated query alongside the results so it can be inspected
    and corrected. Works best over well-labelled data and simple SELECT shapes.
    """
    if not ANTHROPIC_KEY:
        return {
            "error": True,
            "detail": "nl_query needs ANTHROPIC_API_KEY; write SPARQL directly instead",
        }

    schema = await _schema_digest(graph)
    query = await _translate(question, schema, graph)
    if not query:
        return {"error": True, "detail": "translation produced no query"}

    results = await _call(
        "POST", "/sparql/select", json_body={"query": query, "graph": graph}
    )
    return {"query": query, "results": results}


async def _schema_digest(graph: str | None) -> str:
    """Sample classes and predicates so the model writes against real terms."""
    scope = f"GRAPH <{graph}>" if graph else "GRAPH ?g"
    classes = await _call(
        "POST",
        "/sparql/select",
        json_body={
            "query": f"""SELECT ?class (COUNT(?s) AS ?n) WHERE {{
  {scope} {{ ?s a ?class }}
}} GROUP BY ?class ORDER BY DESC(?n) LIMIT 40"""
        },
    )
    predicates = await _call(
        "POST",
        "/sparql/select",
        json_body={
            "query": f"""SELECT ?p (COUNT(*) AS ?n) WHERE {{
  {scope} {{ ?s ?p ?o }}
}} GROUP BY ?p ORDER BY DESC(?n) LIMIT 60"""
        },
    )
    return json.dumps({"classes": classes, "predicates": predicates})[:12000]


async def _translate(question: str, schema: str, graph: str | None) -> str | None:
    system = (
        "You translate questions into SPARQL 1.1 SELECT queries for an RDF store. "
        "Use only the classes and predicates listed in the supplied schema digest. "
        "Wrap patterns in a GRAPH clause. Return the query alone, with no prose "
        "and no code fences."
    )
    target = f"The relevant named graph is <{graph}>." if graph else ""
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 1200,
                "system": system,
                "messages": [
                    {
                        "role": "user",
                        "content": f"Schema digest:\n{schema}\n\n{target}\n\nQuestion: {question}",
                    }
                ],
            },
        )
    if response.status_code >= 400:
        return None
    blocks = response.json().get("content", [])
    text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    return text.replace("```sparql", "").replace("```", "").strip() or None


# --- named queries ------------------------------------------------------------


@mcp.tool()
async def list_named_queries(
    vocabulary: str | None = None, filter: str | None = None
) -> dict:
    """List registered named queries with their parameters.

    Each entry reports its vocabulary: ``hquery`` queries take ordinary SPARQL
    variables bound through a VALUES clause, ``hb`` queries take
    ``{{placeholder}}`` substitution. Filter by ``hb`` or ``hquery``.
    """
    params = {k: v for k, v in {"vocabulary": vocabulary, "filter": filter}.items() if v}
    return await _call("GET", "/named-queries", params=params or None)


@mcp.tool()
async def get_named_query(query_id: str) -> dict:
    """Full definition of one named query, including its SPARQL body."""
    return await _call("GET", f"/named-query/{query_id}")


@mcp.tool()
async def run_named_query(
    query_id: str,
    params: dict | None = None,
    dry_run: bool = False,
    graph: str | None = None,
) -> dict:
    """Run a named query with parameters.

    Parameter datatypes come from the registry, so supply plain values and let
    the bridge render them. Unsupplied optional parameters stay unbound, which
    for hquery: queries means "match all". Set ``dry_run`` to see the bound
    SPARQL without executing it.
    """
    return await _call(
        "POST",
        f"/named-query/{query_id}/run",
        json_body={"params": params or {}, "dry_run": dry_run, "graph": graph},
    )


@mcp.tool()
async def reload_named_queries() -> dict:
    """Re-read the named-query registry, discarding the cached copy."""
    return await _call("POST", "/named-queries/reload")


# --- named rules --------------------------------------------------------------


@mcp.tool()
async def list_named_rules(rule_status: str | None = None) -> dict:
    """List registered named rules with their target graphs and write modes.

    Filter by ``Active``, ``Suspended``, or ``Deprecated``. Rules run in
    declared ``order``; a rule with no order runs after those that have one.
    """
    params = {"rule_status": rule_status} if rule_status else None
    return await _call("GET", "/named-rules", params=params)


@mcp.tool()
async def get_named_rule(rule_id: str) -> dict:
    """Full definition of one named rule, including its CONSTRUCT body."""
    return await _call("GET", f"/named-rule/{rule_id}")


@mcp.tool()
async def run_named_rule(
    rule_id: str,
    params: dict | None = None,
    write_mode: str | None = None,
    target_graph: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Run a named rule, materialising its CONSTRUCT into the target graph.

    ``write_mode`` overrides the rule's own: ``Append`` adds, ``Replace``
    makes the target exactly this output, ``Sync`` reconciles — inserting what
    is newly derived and removing what the rule no longer derives. Sync is the
    mode that makes a rule safely re-runnable. Supply ``$this`` in ``params``
    to bind a focus node. Suspended and deprecated rules are refused.

    ``target_graph`` overrides the rule's own registered target for this run
    only. Point a rule at a scratch graph to materialise its output somewhere
    inspectable without touching what it would normally write to — useful for
    seeing what a rule would produce, or for running a reduction rule against
    a candidate state before deciding whether to commit it.
    """
    return await _call(
        "POST",
        f"/named-rule/{rule_id}/run",
        json_body={
            "params": params or {},
            "write_mode": write_mode,
            "target_graph": target_graph,
            "dry_run": dry_run,
        },
    )


@mcp.tool()
async def run_all_named_rules(
    params: dict | None = None, stop_on_error: bool = True
) -> dict:
    """Fire every active rule once, in order.

    A single pass, not a fixpoint. A self-feeding rule — transitive closure,
    for instance — needs calling repeatedly until ``triplesAdded`` is zero.
    """
    return await _call(
        "POST",
        "/named-rules/run",
        json_body={"params": params or {}, "stop_on_error": stop_on_error},
    )


@mcp.tool()
async def reload_named_rules() -> dict:
    """Re-read the named-rule registry, discarding the cached copy."""
    return await _call("POST", "/named-rules/reload")


@mcp.tool()
async def graph_op(
    operation: str, target: str, source: str | None = None, silent: bool = True
) -> dict:
    """Run a SPARQL graph-management operation.

    One of ``clear``, ``drop``, ``create`` (target only) or ``copy``, ``move``,
    ``add`` (source and target). These are what the rule write modes are built
    from, and are available directly.
    """
    return await _call(
        "POST",
        "/graph-op",
        json_body={
            "operation": operation,
            "target": target,
            "source": source,
            "silent": silent,
        },
    )


# --- pipelines and ingestion --------------------------------------------------


@mcp.tool()
async def list_pipelines() -> dict:
    """List registered pipeline manifests."""
    return await _call("GET", "/pipelines")


@mcp.tool()
async def get_pipeline(pipeline_id: str) -> dict:
    """One manifest: its nodes, resolved run order, and any warnings.

    ``runnable`` is false when the dependency graph has a cycle; the error
    names the stages involved.
    """
    return await _call("GET", f"/pipeline/{pipeline_id}")


@mcp.tool()
async def register_pipeline(
    pipeline_id: str, manifest: str, label: str | None = None
) -> dict:
    """Register a manifest (Turtle in the build: vocabulary) into its own graph."""
    return await _call(
        "POST",
        "/pipeline",
        json_body={"id": pipeline_id, "manifest": manifest, "label": label},
    )


@mcp.tool()
async def run_pipeline(
    pipeline_id: str,
    params: dict | None = None,
    wait: bool = False,
    stop_on_error: bool = True,
) -> dict:
    """Run a pipeline in dependency order.

    Returns a message id immediately; poll ``get_message``. Set ``wait`` to
    run inline and get the finished message instead. Stages with ``llm``,
    ``human``, or ``external`` transformers are recorded as Deferred — the
    bridge does not execute them.
    """
    return await _call(
        "POST",
        "/pipeline-run",
        json_body={
            "pipeline": pipeline_id,
            "params": params or {},
            "wait": wait,
            "stop_on_error": stop_on_error,
        },
    )


@mcp.tool()
async def ingest(
    graph_iri: str | None = None,
    turtle: str | None = None,
    databook: str | None = None,
    source_graph: str | None = None,
    source_url: str | None = None,
    shapes_graph: str | None = None,
    mode: str = "merge",
    reduction_rule_id: str | None = None,
    pipeline: str | None = None,
    wait: bool = False,
) -> dict:
    """Land a payload in a named graph, then optionally run a pipeline.

    Supply exactly one of ``turtle`` (inline Turtle), ``databook`` (inline
    DataBook document — extracts the primary ``turtle``/``turtle12`` block),
    ``source_graph`` (already in the store), or ``source_url`` (fetched once,
    then sniffed as a DataBook or raw Turtle). Validation runs under the same
    gate as push, so ingestion is not a way around it — including
    ``reduction_rule_id``, which reduces the candidate write to its current
    state before validating, the same as on ``push_turtle``.

    ``graph_iri`` is optional for ``databook`` and ``source_url`` when the
    DataBook itself declares ``graph.named_graph`` in its frontmatter; an
    explicit ``graph_iri`` always wins over that declaration. It stays
    required in practice for ``turtle`` and ``source_graph``, which have no
    frontmatter to supply it from.
    """
    return await _call(
        "POST",
        "/ingest",
        json_body={
            "turtle": turtle,
            "databook": databook,
            "source_graph": source_graph,
            "source_url": source_url,
            "graph_iri": graph_iri,
            "shapes_graph": shapes_graph,
            "mode": mode,
            "reduction_rule_id": reduction_rule_id,
            "pipeline": pipeline,
            "wait": wait,
        },
    )


@mcp.tool()
async def get_message(message_id: str) -> dict:
    """Status of an asynchronous run, including per-stage outcomes."""
    return await _call("GET", f"/message/{message_id}")


@mcp.tool()
async def list_messages(limit: int = 20) -> dict:
    """Recent run records, most recent first."""
    return await _call("GET", "/messages", params={"limit": limit})


# --- scheduler ----------------------------------------------------------------
#
# These always target the admin dataset, whatever dataset the session is
# otherwise using. One scheduler per process means one registry and one
# provenance trail; a per-caller view of either would be a fiction.


@mcp.tool()
async def get_scheduler_status() -> dict:
    """Whether the scheduler is running, and its task and persona counts."""
    return await _call("GET", "/scheduler/status")


@mcp.tool()
async def list_scheduled_tasks(task_status: str | None = None) -> dict:
    """List scheduled tasks. Filter by Active, Suspended, or Deprecated."""
    params = {"task_status": task_status} if task_status else None
    return await _call("GET", "/scheduler/tasks", params=params)


@mcp.tool()
async def create_scheduled_task(
    task_id: str,
    action_class: str = "ReadOnlyQuery",
    interval_seconds: float = 3600,
    dataset_scope: str | None = None,
    persona: str | None = None,
    policy: str | None = None,
    sparql: str | None = None,
    rule: str | None = None,
    pipeline: str | None = None,
    projection: str | None = None,
    maintenance: str | None = None,
    payload: str | None = None,
    target_graph: str | None = None,
    label: str | None = None,
) -> dict:
    """Create a scheduled task.

    Declare exactly one action: ``sparql``, ``rule``, ``pipeline``,
    ``projection`` (fire a projection hook), ``maintenance``
    (``projection-sweep``), or ``payload``.
    ``dataset_scope`` is the dataset the task acts on — distinct from the admin
    dataset its definition lives in. A task naming a persona may only perform
    action classes in that persona's capability set.
    """
    return await _call(
        "POST",
        "/scheduler/task",
        json_body={
            "id": task_id,
            "action_class": action_class,
            "interval_seconds": interval_seconds,
            "dataset_scope": dataset_scope,
            "persona": persona,
            "policy": policy,
            "sparql": sparql,
            "rule": rule,
            "pipeline": pipeline,
            "projection": projection,
            "maintenance": maintenance,
            "payload": payload,
            "target_graph": target_graph,
            "label": label,
        },
    )


@mcp.tool()
async def set_task_status(task_id: str, task_status: str) -> dict:
    """Suspend, resume, or deprecate a task. Only Active tasks fire."""
    return await _call(
        "POST", f"/scheduler/task/{task_id}/status", json_body={"status": task_status}
    )


@mcp.tool()
async def fire_scheduled_task(task_id: str) -> dict:
    """Fire a task now, out of band.

    Tagged ``manual`` in provenance and outside the daily scheduled allowance.
    """
    return await _call("POST", f"/scheduler/task/{task_id}/fire")


@mcp.tool()
async def get_recent_scheduler_activity(
    since: str | None = None, limit: int = 50
) -> dict:
    """Recent firing records, most recent first.

    ``since`` must carry a timezone (``2026-07-27T00:00:00Z``). Without one the
    comparison against stored timestamps is indeterminate within ±14 hours, so
    recent windows come back empty while distant ones work; the bridge refuses
    an unqualified value rather than returning a misleading empty list.
    """
    params = {"limit": limit}
    if since:
        params["since"] = since
    return await _call("GET", "/scheduler/activity", params=params)


@mcp.tool()
async def get_quarantined_proposals(limit: int = 50) -> dict:
    """Proposals held back because they failed validation, with their Turtle."""
    return await _call("GET", "/scheduler/quarantine", params={"limit": limit})


@mcp.tool()
async def reload_scheduler() -> dict:
    """Re-read tasks and personas. New tasks are inert until this runs."""
    return await _call("POST", "/scheduler/reload")


# --- projections --------------------------------------------------------------
#
# The graph stays authoritative. A hook computes a scoped slice, diffs it
# against what was last delivered, and hands the difference to a target that
# does its own transformation. The bridge knows nothing about SQL or XSLT.


@mcp.tool()
async def list_projection_hooks(hook_status: str | None = None) -> dict:
    """List projection hooks, with any configuration problems flagged."""
    params = {"hook_status": hook_status} if hook_status else None
    return await _call("GET", "/projection/hooks", params=params)


@mcp.tool()
async def get_projection_hook(hook_id: str) -> dict:
    """One hook, its scope, and how many triples its watermark holds."""
    return await _call("GET", f"/projection/hook/{hook_id}")


@mcp.tool()
async def register_projection_hook(
    hook_id: str,
    target: str,
    scope: str | None = None,
    named_query: str | None = None,
    change_mode: str = "upsert",
    delivery: str = "pull",
    endpoint: str | None = None,
    key_predicate: str | None = None,
) -> dict:
    """Register a projection hook.

    ``scope`` is a CONSTRUCT defining what this target sees, or use
    ``named_query`` to point at a registered one. ``change_mode`` is how the
    target wants change expressed: ``append`` (additions only), ``upsert``
    (keyed by ``key_predicate``), ``soft-delete`` (retractions become
    tombstones), or ``replace`` (whole slice each time). ``delivery`` is
    ``webhook`` (the bridge POSTs to ``endpoint``) or ``pull`` (the target
    collects and acknowledges).
    """
    return await _call(
        "POST",
        "/projection/hook",
        json_body={
            "id": hook_id,
            "target": target,
            "scope": scope,
            "named_query": named_query,
            "change_mode": change_mode,
            "delivery": delivery,
            "endpoint": endpoint,
            "key_predicate": key_predicate,
        },
    )


@mcp.tool()
async def run_projection_hook(
    hook_id: str, force: bool = False, include_payload: bool = True
) -> dict:
    """Compute what changed since the last delivery and hand it over.

    For a ``pull`` hook this returns the envelope and holds it pending; call
    ``acknowledge_projection_delivery`` once the target has applied it. The
    watermark only advances on acknowledgement, so an unacknowledged delivery
    is simply re-derived next run.
    """
    return await _call(
        "POST",
        f"/projection/hook/{hook_id}/run",
        json_body={"force": force, "include_payload": include_payload},
    )


@mcp.tool()
async def reset_projection_hook(hook_id: str) -> dict:
    """Forget what has been delivered, so the next run sends the whole slice."""
    return await _call("POST", f"/projection/hook/{hook_id}/reset")


@mcp.tool()
async def sweep_projection_deliveries(max_age_seconds: float = 86400) -> dict:
    """Reclaim pull deliveries no target ever acknowledged.

    Safe to run often: sweeping leaves the watermark alone, so a swept
    delivery's difference is re-derived on the next run.
    """
    return await _call(
        "POST", "/projection/sweep", json_body={"max_age_seconds": max_age_seconds}
    )


@mcp.tool()
async def list_projection_deliveries(
    hook: str | None = None, delivery_status: str | None = None, limit: int = 50
) -> dict:
    """Delivery records. Filter by hook or by pending/delivered/acknowledged/failed."""
    params = {"limit": limit}
    if hook:
        params["hook"] = hook
    if delivery_status:
        params["delivery_status"] = delivery_status
    return await _call("GET", "/projection/deliveries", params=params)


@mcp.tool()
async def get_projection_delivery(
    delivery_id: str, include_payload: bool = True
) -> dict:
    """One delivery; a pending one comes back with its envelope."""
    return await _call(
        "GET",
        f"/projection/delivery/{delivery_id}",
        params={"include_payload": include_payload},
    )


@mcp.tool()
async def acknowledge_projection_delivery(delivery_id: str) -> dict:
    """Confirm the target applied this envelope. The watermark advances."""
    return await _call("POST", f"/projection/delivery/{delivery_id}/ack")


@mcp.tool()
async def reject_projection_delivery(
    delivery_id: str, reason: str = "rejected by target"
) -> dict:
    """Report that the target could not apply it. The watermark stays put, so
    the same difference is offered again on the next run."""
    return await _call(
        "POST", f"/projection/delivery/{delivery_id}/reject", json_body={"reason": reason}
    )


# --- datasets -----------------------------------------------------------------


@mcp.tool()
async def list_datasets() -> dict:
    """List the datasets the backend actually hosts.

    Different question from ``list_banks``, which reports the bridge's
    configured banks. A bank names a dataset it expects; this says what is
    really there. ``active`` marks the one calls are currently going to.
    """
    result = await _call("GET", "/datasets")
    if isinstance(result, dict) and not result.get("error"):
        result["active"] = _dataset_override or result.get("active")
        result["datasetOverrideSource"] = _dataset_override_source
    return result


@mcp.tool()
async def switch_dataset(name: str) -> dict:
    """Point every subsequent call at a different dataset.

    Validates first: switching to a name the server does not host would
    otherwise produce empty reads that look exactly like empty data, which is
    a slow way to find a typo. Sets ``X-Dataset-Override`` on every following
    request, so it needs no restart and no second bridge process.

    Persisted to disk, not just held in memory — a process restart restores
    this choice instead of silently falling back to whatever
    ``HOLONBRIDGE_DATASET`` (or nothing) says, which is exactly the failure
    that motivated this. An explicit ``HOLONBRIDGE_DATASET`` env var still
    wins over the persisted value on the *next* restart, the same as every
    other setting in this codebase — persistence is a convenience for the
    ordinary case, not a way to override a deliberate pin.

    Pass an empty string to clear the override and fall back to the active
    bank's own dataset. Clearing also removes the persisted file, so a
    restart after clearing starts genuinely fresh rather than restoring the
    just-cleared value.
    """
    global _dataset_override, _dataset_override_source

    if not name.strip():
        _dataset_override = ""
        _dataset_override_source = "none"
        _persist_dataset("")
        endpoint = await _call("GET", "/endpoint")
        return {
            "ok": True,
            "cleared": True,
            "dataset": endpoint.get("dataset") if isinstance(endpoint, dict) else None,
            "note": "override cleared; using the active bank's dataset",
        }

    check = await _call("GET", f"/datasets/{name.strip()}")
    if isinstance(check, dict) and check.get("error"):
        return {
            "ok": False,
            "requested": name,
            "detail": check.get("detail"),
            "note": "dataset not switched",
        }

    _dataset_override = name.strip()
    _dataset_override_source = "explicit"
    _persist_dataset(_dataset_override)
    return {
        "ok": True,
        "dataset": _dataset_override,
        "graphs": check.get("graphs") if isinstance(check, dict) else None,
    }


# --- banks --------------------------------------------------------------------


@mcp.tool()
async def list_banks() -> dict:
    """List the banks the bridge has configured, and which one this process uses.

    A *bank* is a named backend connection — a server URL, a default dataset,
    and optional credentials. Formerly called a "profile".

    ``active`` in the bridge's own response is the bank the *bridge* treats as
    current. ``bankOverride`` is this MCP process's own per-call override,
    which takes precedence for every call made from here without changing
    anything for other clients.
    """
    result = await _call("GET", "/endpoints")
    if isinstance(result, dict) and not result.get("error"):
        result["bankOverride"] = _bank_override
        result["bankOverrideSource"] = _bank_override_source
    return result


@mcp.tool()
async def switch_bank(name: str) -> dict:
    """Point every subsequent call from this process at a different bank.

    Validates first: switching to a bank the bridge has not configured would
    otherwise produce 404s, or worse, silently fall through to the wrong
    store. Applied as ``?bank=`` on every following request, so it needs no
    restart and no second bridge process.

    Different from ``set_endpoint``, deliberately. ``set_endpoint`` changes
    the bridge's *own* active bank, which every client then sees; this changes
    only what this MCP process asks for, leaving other clients alone. Prefer
    this one unless the intent really is to move the whole bridge.

    Persisted to disk on the same reasoning as ``switch_dataset``: a process
    restart restores this choice rather than silently reverting, because a
    call that reaches the wrong store still succeeds and still returns data.
    An explicit ``HOLONBRIDGE_BANK`` env var still wins on the next restart.

    Pass an empty string to clear the override and fall back to whatever the
    bridge considers active. Clearing removes the persisted file too.
    """
    global _bank_override, _bank_override_source

    if not name.strip():
        _bank_override = ""
        _bank_override_source = "none"
        _persist_bank("")
        endpoint = await _call("GET", "/endpoint")
        return {
            "ok": True,
            "cleared": True,
            "bank": endpoint.get("bank") if isinstance(endpoint, dict) else None,
            "note": "override cleared; using the bridge's active bank",
        }

    wanted = name.strip()
    listing = await _call("GET", "/endpoints")
    if isinstance(listing, dict) and listing.get("error"):
        return {
            "ok": False,
            "requested": wanted,
            "detail": listing.get("detail"),
            "note": "bank not switched; could not read the bank list",
        }

    known = [
        b.get("name")
        for b in (listing.get("banks") or [])
        if isinstance(b, dict)
    ]
    if wanted not in known:
        return {
            "ok": False,
            "requested": wanted,
            "known": known,
            "note": "bank not switched; no bank by that name is configured",
        }

    _bank_override = wanted
    _bank_override_source = "explicit"
    _persist_bank(_bank_override)
    return {"ok": True, "bank": _bank_override, "known": known}


# --- sequences ----------------------------------------------------------------


@mcp.tool()
async def mint_sequence_id(
    name: str,
    purpose: str,
    authorised_by: str | None = None,
    prefix: str | None = None,
    pad: int = 4,
) -> dict:
    """Mint the next identifier from a named sequence counter.

    ``purpose`` is required and becomes part of the MintedIdentifier
    provenance record the bridge writes alongside the counter update —
    it's what makes a minted id auditable on its own, independent of
    whatever consumed it. ``authorised_by`` optionally names the actor
    or process responsible for the mint, as an IRI.
    """
    return await _call(
        "POST",
        "/sequence/mint",
        json_body={
            "name": name,
            "purpose": purpose,
            "authorised_by": authorised_by,
            "prefix": prefix,
            "pad": pad,
        },
    )


def main() -> None:  # pragma: no cover
    """Run the stdio transport. For the full CLI see ``__main__.py``."""
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
