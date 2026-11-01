"""What a person must be able to do for a trigger to be theirs.

A trigger runs as the bridge, so "may this person define it" is answered by
asking whether the person could do, themselves, what the trigger will do:
read what its condition and rule read, and write where it writes. The same
questions are asked twice. At **registration** (and re-activation) they decide
whether the trigger is accepted. At **firing** they are asked again of the
person who last armed it (``hev:armedBy``), because access is not permanent:
someone who has since lost the write grant, or been removed, must not keep
acting through a trigger they set up while they had it.

No HTTP in here. Failures are :class:`TriggerDenied`, carrying the status and
detail the routes turn into a response; the firing path turns the same
exception into a recorded skip.

Persona reachability (``toolset.resolve_reachable``) is deliberately *not* part
of this module: it depends on the caller's current persona, which is session
state, and a firing has no session. It stays a registration-time check in
``routes/triggers.py``; what is re-checked at firing is the part that is a fact
about the store (grants, delegations, roles), not about a session.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from .acl import authorize_query, check_replace, check_write, is_admin
from .conn import Conn
from .named_queries import NamedQuery, apply_query_params
from .named_rules import NamedRule, RuleError, bind_rule
from .params import ParameterError
from .sparql_kind import form

QueryFn = Callable[[str], Awaitable[dict]]

#: Stands in for the focus node when a rule is bound only to be inspected.
PROBE_FOCUS = "urn:holonbridge:trigger-probe"

#: Write modes that reconcile a whole graph rather than add to it.
WHOLESALE_MODES = {"replace", "sync", "supersede"}


class TriggerDenied(Exception):
    """A check failed. ``status`` is the HTTP status a route should use and
    ``detail`` its body (a string or a dict); ``reason`` is the short form for
    logs and the audit trail."""

    def __init__(self, status: int, detail: Any, reason: str = "") -> None:
        super().__init__(reason or str(detail))
        self.status = status
        self.detail = detail
        self.reason = reason or (detail if isinstance(detail, str) else str(detail))


async def require_grant(
    query_fn: QueryFn, conn: Conn, person: str | None, graph: str, *, replace: bool = False
) -> None:
    if person is None:
        raise TriggerDenied(401, "unresolved identity")
    check = check_replace if replace else check_write
    decision = await check(query_fn, conn.holons_graph, person=person, target=graph)
    if not decision.allowed:
        raise TriggerDenied(
            403, f"{decision.reason} (graph: {graph})", reason=decision.reason
        )


async def authorize_firing(
    query_fn: QueryFn, conn: Conn, person: str | None, *, rule: NamedRule, review_required: bool
) -> None:
    """The write a firing performs. A reviewed trigger only stages a candidate
    (approving it is its own gated write); an auto-executing one writes the
    rule's target graph through the rule's own write mode."""
    if review_required:
        await require_grant(query_fn, conn, person, conn.graph("candidates"))
        return
    target = rule.target_graph
    if not target:
        raise TriggerDenied(
            400,
            {
                "error": "rule_has_no_target",
                "message": f"rule {rule.id!r} names no target graph, so it cannot run unreviewed",
            },
        )
    await require_grant(query_fn, conn, person, target)
    if rule.write_mode.lower() in WHOLESALE_MODES:
        await require_grant(query_fn, conn, person, target, replace=True)


async def authorize_reads(
    query_fn: QueryFn, conn: Conn, person: str | None, *, label: str, ident: str, sparql: str
) -> None:
    """May this person read what ``sparql`` reads? Admins bypass."""
    if person is None:
        raise TriggerDenied(401, "unresolved identity")
    if await is_admin(query_fn, conn.holons_graph, person=person):
        return
    decision = await authorize_query(
        query_fn,
        conn.holons_graph,
        person=person,
        sparql_text=sparql,
        persona_of_graph=conn.persona_for_graph,
    )
    if not decision.allowed:
        raise TriggerDenied(
            403,
            {
                "error": f"{label}_not_readable",
                "message": f"{label} {ident!r} reads something this caller may not read: "
                f"{decision.reason}",
            },
            reason=decision.reason,
        )


def bound_condition(condition: NamedQuery) -> str:
    try:
        return apply_query_params(condition, {}).sparql
    except ParameterError as exc:
        raise TriggerDenied(
            400, {"error": "condition_needs_parameters", "message": str(exc)}
        ) from exc


def bound_rule(rule: NamedRule) -> str:
    try:
        return bind_rule(rule, {"$this": PROBE_FOCUS})
    except (RuleError, ParameterError) as exc:
        raise TriggerDenied(400, {"error": "rule_not_bindable", "message": str(exc)}) from exc


async def authorize_definition(
    query_fn: QueryFn,
    conn: Conn,
    person: str | None,
    *,
    condition: NamedQuery,
    rule: NamedRule,
    review_required: bool,
) -> list[str]:
    """Everything a trigger would do as the bridge, checked against ``person``.
    Returns non-fatal warnings."""
    if person is None:
        raise TriggerDenied(401, "unresolved identity")
    warnings: list[str] = []

    condition_sparql = bound_condition(condition)
    if form(condition_sparql) != "SELECT":
        raise TriggerDenied(
            400,
            {
                "error": "condition_not_select",
                "message": f"condition {condition.id!r} is a {form(condition_sparql)}; "
                "a trigger condition must be a SELECT projecting ?focus",
            },
        )
    if "?focus" not in condition_sparql and "$focus" not in condition_sparql:
        warnings.append(f"condition {condition.id!r} never mentions ?focus; it will match nothing")
    if re.search(r"\bLIMIT\b", condition_sparql, re.IGNORECASE):
        warnings.append(
            f"condition {condition.id!r} carries a LIMIT; a truncated result looks like a "
            "falling edge and re-arms the rows it cut off"
        )

    rule_sparql = bound_rule(rule)
    if form(rule_sparql) not in {"CONSTRUCT", "DESCRIBE"}:
        raise TriggerDenied(
            400,
            {
                "error": "rule_not_construct",
                "message": f"rule {rule.id!r} is a {form(rule_sparql)}; a rule must CONSTRUCT",
            },
        )
    if not rule.runnable:
        warnings.append(
            f"rule {rule.id!r} is {rule.status.lower()}; the trigger will fail until it is Active"
        )

    # The trigger reads both as the bridge, so the definer must be able to.
    await authorize_reads(
        query_fn, conn, person, label="condition", ident=condition.id, sparql=condition_sparql
    )
    await authorize_reads(
        query_fn, conn, person, label="rule", ident=rule.id, sparql=rule_sparql
    )
    await authorize_firing(query_fn, conn, person, rule=rule, review_required=review_required)
    return warnings
