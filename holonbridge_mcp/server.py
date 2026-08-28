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

CHANGED 2026-08-15: ``_headers()`` now adds ``X-Holon-Animus-Id`` /
``X-Holon-Animus-Type`` when :data:`holonbridge_mcp.identity.current_github_login`
carries a verified identity — the REST bridge's ACL layer resolves that
header to a Person and checks their Role grants before allowing a read,
write, or named-query invocation. Absent (stdio transport, or the remote
transport's static-token credential, which by design has no per-user
identity) means no animus header is sent, and the bridge's own
``require_animus`` dependency is what turns that into a clean 401 rather
than a silent bypass.

CHANGED 2026-08-17: added ``switch_persona``/``list_personas``. Unlike
``switch_dataset``/``switch_bank`` just below, these hold no module-level
override state here — persona is per-person, not per-process (two logins
through one MCP process must never move each other's persona), so the
bridge itself owns that state, keyed by the Animus.person your credential
resolves to. This layer stays a thin pass-through for it, same as
``whoami``.
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

from .identity import current_github_login

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
    login = current_github_login.get()
    if login:
        headers["X-Holon-Animus-Id"] = login
        headers["X-Holon-Animus-Type"] = "GitHubIdentity"
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


# --- identity -------------------------------------------------------------


@mcp.tool()
async def whoami() -> dict:
    """Which Person the bridge resolved this session's credential as.

    Requires nothing beyond a resolved identity -- no grant on anything.
    Exists because every founder currently holds identical grants, so no
    permission-based test can tell them apart; this answers "who am I"
    directly instead of by inference from what does or doesn't succeed.

    Also reports ``persona``/``personaSource`` -- your active persona
    override for the current dataset, and where it came from
    (``explicit``/``persisted``/``env``/``none``). See ``switch_persona``.
    """
    return await _call("GET", "/whoami")


@mcp.tool()
async def switch_persona(name: str = "") -> dict:
    """Switch your active persona for the current dataset.

    Per-person, not per-process -- unlike ``switch_dataset``/``switch_bank``
    below, this holds no state in this MCP process. "Which persona" is a
    question about *who's asking*, so the bridge keys it by the Person your
    credential resolves to (see ``whoami``); switching here can never move
    another caller's persona, and can never be pointed at one.

    Gated on holding a ``holon:Home`` under the named persona -- existence
    of the persona alone is not enough. The response distinguishes a
    misspelled name (``not_found``) from a real persona you're not a
    member of (``refused``); both come back as ``ok: false`` with a
    ``note`` saying which. Call ``list_personas`` first if you're not sure
    which names would actually succeed for you.

    Pass an empty string (or omit ``name``) to clear your override for
    this dataset and fall back to ground truth only.
    """
    return await _call("POST", "/persona/switch", json_body={"name": name})


@mcp.tool()
async def list_personas() -> dict:
    """Every holon:Persona in the current dataset, and whether you hold a
    Home under it -- i.e. which names ``switch_persona`` would actually
    accept from you right now."""
    return await _call("GET", "/persona/list")


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

    Also reports ``personaOverride``/``personaOverrideSource`` — your
    active persona for the current dataset, same values as ``whoami``.
    This route now requires a resolved identity to answer that; a caller
    that used to reach it with only a bearer token needs to start sending
    an animus identity like every other identity-gated tool.
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
async def create_holon(
    databook: str,
    block_id: str | None = None,
    graph_iri: str | None = None,
    shapes_graph: str | None = None,
    mode: str = "merge",
    reduction_rule_id: str | None = None,
) -> dict:
    """Create or merge into a holon from a DataBook message.

    Unlike ``push_turtle``, which takes raw Turtle and an explicit
    ``graph_iri``, this takes a full DataBook (frontmatter plus one or more
    fenced blocks) and extracts the RDF for you -- the first turtle,
    turtle12, or json-ld block, or the one named by ``block_id``. A
    json-ld block is converted to Turtle before it's written; Fuseki only
    ever receives Turtle either way.

    ``graph_iri`` overrides the DataBook's own ``graph.named_graph``
    frontmatter if both are given; one of the two is required, there is
    no default target graph here. Everything else -- ``shapes_graph``,
    ``mode``, ``reduction_rule_id`` -- means exactly what it means on
    ``push_turtle``, because both call the same gated write path on the
    bridge.
    """
    return await _call(
        "POST",
        "/holon",
        json_body={
            "databook": databook,
            "block_id": block_id,
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
async def get_named_query_schema(query_id: str) -> dict:
    """SHACL shape describing one named query's parameters.

    Derived fresh from the same parameter declarations ``run_named_query``
    binds against -- name, datatype-or-IRI, required, default,
    description -- as both a plain parameter list and a ``sh:NodeShape``
    in Turtle, for a client that wants to introspect or auto-generate a
    form without parsing the query body or any ``databook:param``-style
    comments. Gated the same way as ``get_named_query``/``run_named_query``:
    a query outside your reachable set for the current persona comes back
    as unknown, not as a visible-but-forbidden query.
    """
    return await _call("GET", f"/named-query/{query_id}/schema")


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


# --- named triggers -------------------------------------------------------
#
# Added 2026-08-26, alongside PR #11 on the bridge itself. A trigger's
# condition is an ordinary named query (a SELECT projecting ?focus) and its
# action is an ordinary named rule bound with $this=focus -- no new query
# dialect, no new rule mechanism. StateTrigger fires from a fluent.py hook
# after a confirmed transition; TemporalTrigger fires from the scheduler's
# periodic "trigger-sweep" maintenance job. A reviewRequired=true trigger's
# firing stages its rule's literal output as a Turtle candidate (see
# "candidate review queue" below) instead of writing it immediately.
#
# Not yet Toolset/persona-reachability-gated on the bridge side, same as
# named-queries/named-rules were before PR #9 -- routes/triggers.py notes
# this as deferred, not dropped.


@mcp.tool()
async def list_named_triggers(
    trigger_status: str | None = None, refresh: bool = False
) -> dict:
    """List registered named triggers.

    Filter by ``Active``, ``Suspended``, or ``Deprecated``. Each entry
    reports its ``triggerKind`` (``StateTrigger`` or ``TemporalTrigger``),
    its condition (a named-query id) and action (a named-rule id), and
    whether firing stages to the candidate queue (``reviewRequired: true``)
    or runs the rule directly.
    """
    params: dict[str, Any] = {}
    if trigger_status:
        params["trigger_status"] = trigger_status
    if refresh:
        params["refresh"] = refresh
    return await _call("GET", "/named-triggers", params=params or None)


@mcp.tool()
async def get_named_trigger(trigger_id: str) -> dict:
    """Full definition of one named trigger, including its condition and action."""
    return await _call("GET", f"/named-trigger/{trigger_id}")


@mcp.tool()
async def evaluate_named_trigger(
    trigger_id: str, touched_predicates: list[str] | None = None
) -> dict:
    """Evaluate one trigger on demand, out of band from its normal firing path.

    A StateTrigger normally fires from a fluent transition, a TemporalTrigger
    from the scheduler's periodic sweep — this runs the same condition/action
    logic immediately, for testing a newly registered trigger or re-checking
    one manually. ``touched_predicates`` narrows evaluation to triggers that
    declared a matching ``watchedPredicate``; omit it to evaluate regardless
    of that narrowing.

    A ``reviewRequired`` trigger's firing lands in the candidate queue (see
    ``list_candidates``/``get_candidate``) rather than writing immediately —
    check there for the result, not the target graph, until it's approved.
    """
    return await _call(
        "POST",
        f"/named-trigger/{trigger_id}/evaluate",
        json_body={"touched_predicates": touched_predicates},
    )


@mcp.tool()
async def reload_named_triggers() -> dict:
    """Re-read the named-trigger registry, discarding the cached copy."""
    return await _call("POST", "/named-triggers/reload")


# --- candidate review queue -------------------------------------------------
#
# Where a reviewRequired=true trigger's firing lands: the literal Turtle its
# rule would produce, staged for a human to approve or reject rather than
# written immediately. Approval always merges (GSP POST) regardless of the
# rule's own declared write mode -- an unreviewed action that later gets a
# human's approval should never be more destructive than strictly additive,
# since the live graph may have changed between proposal and approval.


@mcp.tool()
async def list_candidates(candidate_status: str | None = None) -> dict:
    """List staged trigger candidates. Filter by Pending, Approved, or Rejected."""
    params = {"candidate_status": candidate_status} if candidate_status else None
    return await _call("GET", "/candidates", params=params)


@mcp.tool()
async def get_candidate(candidate_id: str) -> dict:
    """One candidate's detail, including the literal Turtle it would merge on approval."""
    return await _call("GET", f"/candidate/{candidate_id}")


@mcp.tool()
async def approve_candidate(candidate_id: str) -> dict:
    """Approve a pending candidate: merges its staged Turtle (GSP POST) into
    the target graph, regardless of the underlying rule's own write mode
    (see the section note above). Refused (409) if not Pending."""
    return await _call("POST", f"/candidate/{candidate_id}/approve")


@mcp.tool()
async def reject_candidate(candidate_id: str) -> dict:
    """Reject a pending candidate. No write happens; the candidate is marked
    Rejected. Refused (409) if not Pending."""
    return await _call("POST", f"/candidate/{candidate_id}/reject")


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


# --- events -------------------------------------------------------------------
#
# create_message submits domain-level hev:AssertionEvent content to the
# dataset's events graph. Naming note, deliberately placed right after
# get_message/list_messages above: those report hb:Message pipeline-run
# status (Received/Running/Completed/Failed); this is unrelated -- a
# different graph, a different vocabulary, a different concept, sharing
# the word "message" only by coincidence of two separate naming decisions.
# See holonbridge/routes/events.py's module docstring on the bridge for
# the full explanation. Scope, as of 2026-08-28: AssertionEvent submission
# only -- create_message never invokes a named trigger, a rule, or the
# scheduler.


@mcp.tool()
async def create_message(
    databook: str,
    block_id: str | None = None,
    graph_iri: str | None = None,
    shapes_graph: str | None = None,
    reduction_rule_id: str | None = None,
) -> dict:
    """Submit a DataBook of AssertionEvent content to the events graph.

    Same DataBook-envelope pattern as ``create_holon``: the first turtle/
    turtle12/json-ld block is extracted (or the one named by ``block_id``)
    and written. Two differences from ``create_holon``: the write is
    always a merge -- an event ledger is append-only, there is no
    ``mode`` parameter -- and ``graph_iri`` defaults to the dataset's own
    events graph when neither it nor the DataBook's ``graph.named_graph``
    frontmatter is supplied, which is the common case, not an error.

    Pass ``shapes_graph`` pointing at a registered EventShape if you want
    the bridge to actually enforce ``hev:AssertionEvent`` typing; this
    tool does not check that on its own, only that the payload is
    well-formed RDF.
    """
    return await _call(
        "POST",
        "/message/create",
        json_body={
            "databook": databook,
            "block_id": block_id,
            "graph_iri": graph_iri,
            "shapes_graph": shapes_graph,
            "reduction_rule_id": reduction_rule_id,
        },
    )


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


# --- fluents --------------------------------------------------------------
#
# A fluent's current value lives in a single destructive scene-graph triple
# (or, for list-mode fluents, a destructive set of membership triples),
# rewritten on every transition; every transition is also recorded forever
# as an append-only ledger entry chained to its predecessor. See
# holonbridge.fluent for the full design.
#
# On a dataset with a shapes graph configured, a candidate transition is
# checked on a scratch copy before it ever touches a live graph, so a
# rejected one never appears there, not even briefly. Combined with the
# operation/mode check below, a caller supplying the wrong operation for a
# fluent's mode gets a clean error back, never a corrupted store -- which is
# what makes exposing this directly reasonable, where an earlier revision of
# this module deliberately held off.


@mcp.tool()
async def update_fluent(
    fluent: str,
    operation: str,
    value: Any = None,
    is_iri: bool = False,
    asserted_by: str | None = None,
    description: str | None = None,
) -> dict:
    """Perform one fluent state transition.

    ``operation`` is one of five, and each fluent has a declared mode
    (``holon:fluentOperationMode`` on its ``holon:FluentProperty``) that only
    some operations are valid against:

    - ``Set`` — assign ``value`` outright. Valid for every mode, and doubles
      as initialisation: the first ``Set`` on a fluent with no prior value
      just creates it. ``value`` is the absolute new value.
    - ``Insert`` / ``Remove`` — add or subtract ``value`` as a delta. Only
      valid for ``NumericAccumulator`` and ``DateAccumulator`` fluents, and
      only once the fluent already has a value (``Set`` it first).
    - ``ListInsert`` / ``ListRemove`` — add or remove one member of a
      ``ListAccumulator`` (bag-membership, one-to-many) fluent. ``value`` is
      the member; pass ``is_iri=True`` if it is an entity reference rather
      than a literal.

    A wrong operation for a fluent's mode, or a delta operation on a fluent
    with no current value, is refused before anything is written — never a
    corrupted store, just a clean error. On a dataset with a shapes graph
    configured, the whole candidate transition is additionally checked on a
    scratch copy before it ever touches a live graph.

    Every transition is recorded forever as an append-only ledger entry,
    chained to its predecessor. There is no Clear or Unset — to revert, read
    the prior value with ``get_prior_fluent_value`` and issue an ordinary
    ``Set`` with it.

    Returns ``oldValue``/``newValue`` (each ``{"kind": "uri"|"literal",
    "value": ...}``), the new ledger entry's ``assertion`` IRI, which entry
    it ``superseded`` (``null`` for a fluent's first-ever transition), and
    the minted ``sequenceId``.
    """
    return await _call(
        "POST",
        "/fluent/update",
        json_body={
            "fluent": fluent,
            "operation": operation,
            "value": value,
            "is_iri": is_iri,
            "asserted_by": asserted_by,
            "description": description,
        },
    )


@mcp.tool()
async def get_prior_fluent_value(fluent: str) -> dict:
    """The value a fluent held immediately before its current one.

    Read from the ledger, not the scene graph — for building a Set-based
    revert (``update_fluent`` has no Unset primitive; see its docstring).
    Comes back error-shaped if the fluent has no prior transition (set only
    once, or never set at all).
    """
    return await _call("GET", f"/fluent/{fluent}/prior")


def main() -> None:  # pragma: no cover
    """Run the stdio transport. For the full CLI see ``__main__.py``."""
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
