"""Fluent state-transition tools.

A fluent's current value lives in a single destructive scene-graph triple
(or, for list-mode fluents, a destructive set of membership triples),
rewritten on every transition; every transition is also recorded forever
as an append-only ledger entry chained to its predecessor. See
holonbridge.fluent for the full design.

On a dataset with a shapes graph configured, a candidate transition is
checked on a scratch copy before it ever touches a live graph, so a
rejected one never appears there, not even briefly. Combined with the
operation/mode check below, a caller supplying the wrong operation for a
fluent's mode gets a clean error back, never a corrupted store -- which is
what makes exposing this directly reasonable, where an earlier revision of
this module deliberately held off.
"""

from __future__ import annotations

from typing import Any

from ..session import mcp, _call


@mcp.tool()
async def update_fluent(
    fluent: str,
    operation: str,
    value: Any = None,
    is_iri: bool = False,
    asserted_by: str | None = None,
    description: str | None = None,
    asserted_datetime: str | None = None,
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

    Requires an identity and write access to the events, scene and sequences
    graphs (an administrator has it implicitly). The ledger attributes every
    transition to the person calling: ``asserted_by`` is optional and, if given,
    must be your own IRI, or the call is refused. An administrator who needs to
    assert as someone else uses ``act_as``. IRIs (``fluent``, and ``value`` when
    ``is_iri`` is true) must be plain IRIs — no whitespace or any of
    ``<>"{}|^`\\``.

    ``asserted_datetime`` (ISO 8601, e.g. ``2026-09-01T12:00:00Z``; no timezone
    means UTC) is the *valid time*: when this was true in the world. It
    defaults to now. Pass an earlier time to record a fact you learnt late, such
    as a body found at 23:06 that was dead by 22:41. It does not change when the
    bridge recorded the entry: every ledger entry also carries ``hevt:insertedAt``,
    the wall-clock time of the write, which the caller cannot set. ``get_holon``'s
    ``observed_at`` reads the first axis and ``inserted_at`` the second.

    Returns ``oldValue``/``newValue`` (each ``{"kind": "uri"|"literal",
    "value": ...}``), the new ledger entry's ``assertion`` IRI, which entry
    it ``superseded`` (``null`` for a fluent's first-ever transition), the
    minted ``sequenceId``, and ``assertedBy``.
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
            "asserted_datetime": asserted_datetime,
        },
    )


@mcp.tool()
async def get_prior_fluent_value(fluent: str) -> dict:
    """The value a fluent held immediately before its current one.

    Read from the ledger, not the scene graph — for building a Set-based
    revert (``update_fluent`` has no Unset primitive; see its docstring).
    Comes back error-shaped if the fluent has no prior transition (set only
    once, or never set at all). Requires an identity; ``fluent`` must be a
    plain IRI.
    """
    return await _call("GET", f"/fluent/{fluent}/prior")
