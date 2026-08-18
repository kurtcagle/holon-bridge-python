"""Named-rule routes.

``/graph-op`` lives here rather than with the other graph routes because the
six SPARQL graph-management operations are what the rule write modes are
built from; keeping them together means the semantics have one home.

CHANGED 2026-08-18: list/get/run/run-all now require a resolved identity
(``AnimusDep``) and are gated by Toolset membership, same shape and same
reasoning as ``routes/named_queries.py``'s matching change — see that
module's docstring, including the short-name-vs-full-IRI note for
``bind_persona_param``. ``/graph-op`` is untouched; it isn't part of the
named-rule registry and this design doesn't reach it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..deps import AnimusDep, ClientDep, ConnDep, PersonasDep, RegistryDep, SettingsDep
from ..fuseki import FusekiError, FusekiTimeout
from ..named_rules import (
    RULE_STATUSES,
    WRITE_MODES,
    RuleError,
    RuleSuspended,
    execute_named_rule,
    load_named_rules,
)
from ..params import ParameterError
from ..toolset import bind_persona_param, resolve_reachable

router = APIRouter(tags=["named-rules"])
KIND = "named-rules"

_WRITE_MODE_PATTERN = "^(Append|Replace|Sync|Supersede|append|replace|sync|supersede)$"


class RunRuleRequest(BaseModel):
    params: dict[str, object] = Field(default_factory=dict)
    write_mode: str | None = Field(default=None, pattern=_WRITE_MODE_PATTERN)
    target_graph: str | None = Field(
        default=None,
        description=(
            "Run against this graph instead of the rule's registered target. "
            "The rule's own registration is untouched — this affects only this "
            "one run. Lets a rule be pointed at a scratch graph: reduce a "
            "candidate state before validating it, or materialise a rule's "
            "output somewhere inspectable without touching what it would "
            "normally touch."
        ),
    )
    dry_run: bool = Field(
        default=False, description="return the bound CONSTRUCT without running it"
    )
    timeout: float | None = Field(default=60.0, gt=0, le=900)


class RunAllRequest(BaseModel):
    params: dict[str, object] = Field(default_factory=dict)
    stop_on_error: bool = True
    timeout: float | None = Field(default=60.0, gt=0, le=900)


def _not_found(rule_id: str, available_ids: list[str]) -> HTTPException:
    """Same 404 shape whether the id is genuinely unknown or just outside
    this caller's reachable set — see routes/named_queries.py's matching
    helper; a restricted rule shouldn't differentially reveal its own
    existence either."""
    return HTTPException(
        status.HTTP_404_NOT_FOUND,
        detail={
            "error": "unknown_named_rule",
            "id": rule_id,
            "available": available_ids,
        },
    )


async def _reachable_ids(result, conn, client, persona: str | None) -> set[str]:
    """The subset of `result.rules` (by id) this persona can reach —
    same shared-resolution shape as routes/named_queries.py's helper.
    `persona` is the short name; resolve_reachable converts it itself."""

    async def query_fn(q: str) -> dict:
        return await client.select(conn, q)

    id_by_iri = {r.iri: r.id for r in result.rules}
    reachable_iris = await resolve_reachable(
        query_fn, conn, persona=persona, candidate_iris=list(id_by_iri)
    )
    return {id_by_iri[iri] for iri in reachable_iris}


@router.get("/named-rules")
async def list_named_rules(
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    personas: PersonasDep,
    cache: RegistryDep,
    rule_status: str | None = Query(default=None, pattern="^(Active|Suspended|Deprecated)$"),
    refresh: bool = Query(default=False),
) -> dict:
    result = await cache.get(
        client, conn, kind=KIND, loader=load_named_rules, refresh=refresh
    )

    persona, _source = personas.get(person_id=animus.person, dataset=conn.dataset)
    reachable_ids = await _reachable_ids(result, conn, client, persona)

    rules = [r for r in result.rules if r.id in reachable_ids]
    if rule_status:
        rules = [r for r in rules if r.status == rule_status]
    return {
        "dataset": conn.dataset,
        "graph": conn.graph("named-rules"),
        "count": len(rules),
        "writeModes": list(WRITE_MODES),
        "statuses": list(RULE_STATUSES),
        "rules": [r.summary() for r in rules],
        "warnings": result.warnings,
    }


async def _load_and_authorise(rule_id: str, conn, client, animus, personas, cache):
    """Same shape as routes/named_queries.py's helper of the same name."""
    result = await cache.get(client, conn, kind=KIND, loader=load_named_rules)
    rule = result.by_id(rule_id)
    available_ids = [r.id for r in result.rules]
    if rule is None:
        raise _not_found(rule_id, available_ids)

    persona, _source = personas.get(person_id=animus.person, dataset=conn.dataset)
    reachable_ids = await _reachable_ids(result, conn, client, persona)

    if rule.id not in reachable_ids:
        raise _not_found(rule_id, sorted(reachable_ids))

    return rule, persona


@router.get("/named-rule/{rule_id}")
async def get_named_rule(
    rule_id: str,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    personas: PersonasDep,
    cache: RegistryDep,
) -> dict:
    rule, _persona = await _load_and_authorise(
        rule_id, conn, client, animus, personas, cache
    )
    return rule.detail()


@router.post("/named-rule/{rule_id}/run")
async def run_named_rule(
    rule_id: str,
    body: RunRuleRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    personas: PersonasDep,
    cache: RegistryDep,
) -> dict:
    rule, persona = await _load_and_authorise(
        rule_id, conn, client, animus, personas, cache
    )
    persona_iri = conn.persona_graph(persona) if persona else None
    params = bind_persona_param(
        body.params,
        persona_iri=persona_iri,
        declares_persona="persona" in rule.declared,
    )

    if body.dry_run:
        from ..named_rules import bind_rule  # noqa: PLC0415 - only needed here

        try:
            sparql = bind_rule(rule, params)
        except ParameterError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={"error": "parameter_error", "id": rule_id, "message": str(exc)},
            ) from exc
        return {
            "ruleId": rule.id,
            "targetGraph": body.target_graph or rule.target_graph,
            "writeMode": (body.write_mode or rule.write_mode).capitalize(),
            "executed": False,
            "sparql": sparql,
        }

    return await _run(
        conn, client, rule, params, body.write_mode, body.timeout, body.target_graph
    )


@router.post("/named-rules/run")
async def run_all_named_rules(
    body: RunAllRequest,
    conn: ConnDep,
    client: ClientDep,
    animus: AnimusDep,
    personas: PersonasDep,
    cache: RegistryDep,
) -> dict:
    """Fire every Active rule this caller's persona can reach, once, in
    declared order.

    This is a single pass, not a fixpoint. A rule whose output feeds another
    rule needs the later `order`; a rule that feeds *itself* — transitive
    closure, for instance — needs to be called repeatedly until
    `triplesAdded` reaches zero. That loop is deliberately the caller's, so
    a non-terminating rule cannot hang the bridge.

    Toolset-restricted rules outside the caller's reachable set are
    skipped the same as a Suspended/Deprecated one — counted in `skipped`,
    never run, never an error. A bulk "run everything" shouldn't be a way
    to invoke a rule a targeted `run_named_rule` call would have refused.
    """
    result = await cache.get(client, conn, kind=KIND, loader=load_named_rules)
    persona, _source = personas.get(person_id=animus.person, dataset=conn.dataset)
    reachable_ids = await _reachable_ids(result, conn, client, persona)
    persona_iri = conn.persona_graph(persona) if persona else None

    active = [r for r in result.rules if r.runnable and r.id in reachable_ids]

    runs: list[dict] = []
    errors: list[dict] = []
    for rule in active:
        rule_params = bind_persona_param(
            body.params, persona_iri=persona_iri, declares_persona="persona" in rule.declared
        )
        try:
            runs.append(
                await _run(conn, client, rule, rule_params, None, body.timeout)
            )
        except HTTPException as exc:
            errors.append({"ruleId": rule.id, "detail": exc.detail})
            if body.stop_on_error:
                break

    return {
        "dataset": conn.dataset,
        "pass": "single",
        "ran": len(runs),
        "skipped": len(result.rules) - len(active),
        "results": runs,
        "errors": errors,
        "warnings": result.warnings,
    }


@router.post("/named-rules/reload")
async def reload_named_rules(
    conn: ConnDep, client: ClientDep, cache: RegistryDep
) -> dict:
    """Registry cache maintenance, not tool access — deliberately left
    ungated, same reasoning as named-queries' matching route."""
    cache.invalidate(conn, KIND)
    result = await cache.get(
        client, conn, kind=KIND, loader=load_named_rules, refresh=True
    )
    return {
        "ok": True,
        "dataset": conn.dataset,
        "count": len(result.rules),
        "warnings": result.warnings,
    }


async def _run(conn, client, rule, params, write_mode, timeout, target_graph=None) -> dict:  # noqa: ANN001
    try:
        run = await execute_named_rule(
            conn,
            client,
            rule,
            params=params,
            write_mode=write_mode,
            target_graph=target_graph,
            timeout=timeout,
        )
    except RuleSuspended as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"error": "rule_not_active", "id": rule.id, "message": str(exc)},
        ) from exc
    except ParameterError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "parameter_error", "id": rule.id, "message": str(exc)},
        ) from exc
    except RuleError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"error": "rule_error", "id": rule.id, "message": str(exc)},
        ) from exc
    except FusekiTimeout as exc:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"error": "rule_timeout", "id": rule.id, "message": str(exc)},
        ) from exc
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {**run.as_dict(), "executed": True}


# --- graph operations ---------------------------------------------------------


class GraphOpRequest(BaseModel):
    operation: str = Field(..., pattern="^(clear|drop|create|copy|move|add)$")
    target: str = Field(..., min_length=1)
    source: str | None = None
    silent: bool = True


@router.post("/graph-op")
async def graph_op(
    body: GraphOpRequest, conn: ConnDep, client: ClientDep, settings: SettingsDep
) -> dict:
    """CLEAR, DROP, CREATE, COPY, MOVE, or ADD a named graph."""
    op = body.operation.lower()
    silent = "SILENT " if body.silent else ""

    if op in {"copy", "move", "add"} and not body.source:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"source is required for {op}"
        )

    if op in {"clear", "drop", "create"}:
        keyword = {"clear": "CLEAR", "drop": "DROP", "create": "CREATE"}[op]
        update = f"{keyword} {silent}GRAPH <{body.target}>"
    else:
        keyword = op.upper()
        update = f"{keyword} {silent}<{body.source}> TO <{body.target}>"

    try:
        await client.update(conn, update)
    except FusekiError as exc:
        raise HTTPException(exc.status or 502, detail=exc.as_dict()) from exc

    return {
        "ok": True,
        "operation": op,
        "source": body.source,
        "target": body.target,
        "dataset": conn.dataset,
        "update": update,
    }
