"""The agent action of a named trigger (``hev:onFireAgent``).

A rule-based trigger stages the output of a CONSTRUCT. An agent trigger instead
*places a call*: for each focus its condition matches, one ``hb:AgentCall`` is
recorded (never two while an earlier one for the same focus is still open), as
the person who armed the trigger, through exactly the checks they would face
placing it by hand.

Whether the call then waits for a person depends on two independent switches,
and either one is enough to hold it:

* the trigger's ``reviewRequired`` (default true), and
* the endpoint's ``callReview`` (default true).

Only a trigger registered with ``reviewRequired=false`` *and* aimed at an
endpoint with ``callReview=false`` queues a call as ``Approved`` for the
scheduler's ``agent-dispatch`` job. The endpoint's owner can therefore always
insist on a person, whoever wrote the trigger.

This module has no HTTP in it. The routes and the evaluator call in.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ..conn import Conn
from ..fuseki import FusekiClient
from ..named_queries import NamedQuery
from ..trigger_authz import PROBE_FOCUS, TriggerDenied, authorize_reads, bound_condition
from . import service
from .ledger import CallStore
from .model import APPROVAL_HUMAN, APPROVAL_POLICY, Call, Endpoint
from .policy import check_agent_endpoint

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from ..triggers import Trigger

#: Why no call was placed for a focus; the evaluator records these as skips.
OPEN_CALL = "an earlier call for this focus is still open"


def _no_agent(trigger_id: str, endpoint_id: str, available: list[str]) -> TriggerDenied:
    return TriggerDenied(
        400,
        {
            "error": "unknown_agent_endpoint",
            "message": f"trigger {trigger_id!r} names agent endpoint {endpoint_id!r}, which is not registered",
            "available": available,
        },
    )


async def authorize_agent_trigger(
    query_fn,
    conn: Conn,
    person: str | None,
    *,
    condition: NamedQuery,
    endpoint: Endpoint | None,
    endpoint_id: str,
    context_query: NamedQuery | None,
    target_graph: str,
    review_required: bool,
    settings: "Settings | None" = None,
    known_endpoints: list[str] | None = None,
) -> list[str]:
    """What the definer must be able to do for an agent trigger to be theirs.

    Asked at registration, on re-activation and (cached) at firing. Returns
    non-fatal warnings.
    """
    if person is None:
        raise TriggerDenied(401, "unresolved identity")
    if endpoint is None:
        raise _no_agent("", endpoint_id, known_endpoints or [])

    warnings: list[str] = []
    if not endpoint.active:
        warnings.append(
            f"endpoint {endpoint.id!r} is {endpoint.status.lower()}; the trigger will fail until it is Active"
        )
    else:
        try:
            await check_agent_endpoint(
                endpoint.url, has_credentials=endpoint.has_credentials, settings=settings
            )
        except Exception as exc:  # EndpointRefused
            raise TriggerDenied(
                400,
                {"error": "agent_endpoint_refused", "message": f"endpoint {endpoint.id!r}: {exc}"},
                reason=str(exc),
            ) from exc

    condition_sparql = bound_condition(condition)
    await authorize_reads(
        query_fn, conn, person, label="condition", ident=condition.id, sparql=condition_sparql
    )

    context_sparql = None
    if context_query is not None:
        try:
            context_sparql = service.bind_context(context_query, PROBE_FOCUS)
        except service.CallInvalid as exc:
            raise TriggerDenied(
                400, {"error": "context_not_bindable", "message": str(exc)}, reason=str(exc)
            ) from exc

    await service.authorize_request(
        query_fn,
        conn,
        person,
        context_sparql=context_sparql,
        context_id=context_query.id if context_query else "",
        target_graph=target_graph or None,
    )

    if not review_required and endpoint.call_review:
        warnings.append(
            f"endpoint {endpoint.id!r} requires a person to approve every call, so this "
            "trigger's calls will wait for one despite reviewRequired=false"
        )
    return warnings


def approval_for(trigger: "Trigger") -> str:
    return APPROVAL_HUMAN if trigger.review_required else APPROVAL_POLICY


async def fire_agent(
    client: FusekiClient,
    conn: Conn,
    trigger: "Trigger",
    focus: str,
    *,
    endpoints: Mapping[str, Endpoint],
    queries,
    store: CallStore | None = None,
    settings: "Settings | None" = None,
) -> Call | None:
    """Place the call for one focus. ``None`` means one is already open.

    Raises :class:`service.CallInvalid` / :class:`TriggerDenied` when the call
    cannot be placed (unknown endpoint, definer no longer allowed...), which the
    evaluator records as a failed firing.
    """
    store = store or CallStore(client)
    if await store.open_call_exists(conn, trigger_iri=trigger.iri, focus=focus):
        return None

    endpoint = endpoints.get(trigger.agent_endpoint)
    if endpoint is None:
        raise service.CallInvalid(f"unknown agent endpoint {trigger.agent_endpoint!r}")
    if not trigger.armed_by:
        raise service.CallInvalid(
            "an agent trigger needs a recorded definer to place calls as; re-register it"
        )
    context_query = None
    if trigger.agent_context:
        context_query = queries.by_id(trigger.agent_context)
        if context_query is None:
            raise service.CallInvalid(f"unknown context query {trigger.agent_context!r}")

    return await service.request_call(
        client,
        conn,
        requested_by=trigger.armed_by,
        endpoint=endpoint,
        task=trigger.agent_task,
        focus=focus,
        context_query=context_query,
        target_graph=trigger.agent_target_graph or None,
        approval=approval_for(trigger),
        via_trigger=trigger.iri,
        store=store,
        settings=settings,
    )
