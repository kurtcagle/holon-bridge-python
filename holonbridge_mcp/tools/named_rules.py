"""Named-rule tools, plus the raw graph_op these write modes are built from."""

from __future__ import annotations

from ..session import mcp, _call


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
