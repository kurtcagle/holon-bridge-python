"""Audit log: who did what through the bridge, and what was refused.

Why this exists (context for anyone opening this cold): a write that goes
through ``write_turtle_to_graph`` (or ``/sparql/update``, ``/graph-op``,
``DELETE /graph``, the admin routes ...) leaves no trace of *who* made it,
under what authority, or that it happened at all -- an admin write, a
delegated write and the owner's own write are indistinguishable in the
stored data. Refusals are invisible too. ``holon:AssertionEvent`` (see
``routes/events.py``) is not the answer: those are *domain* events, submitted
by callers as content. An audit entry is a record about the bridge's own
behaviour, written by the bridge, and it must not share a graph or a
vocabulary with domain events.

Design, briefly:

- One ASGI middleware (:class:`AuditMiddleware`) sees every request and its
  outcome, so there is no per-route hook to forget. It records every
  mutation, every 401/403, and -- at the ``standard`` level -- "security
  reads" (a read under ``act_as``, ``on_behalf_of``, or of another
  person's private persona graph).
- Handlers that know more (the target graph, what authorised the write, a
  payload digest) call :func:`annotate`; the middleware folds it into the
  entry. Annotation is a no-op outside a request, so nothing else changes.
- Entries go to a graph of their own, ``urn:{dataset}:audit`` (derived from
  the holons graph the same way ``acl._admin_role`` derives the admin
  role). The bridge writes it through its own client; **nobody** can write
  it through the API, admin included (``acl.check_write``/``check_replace``
  refuse it before the admin bypass), and only admins can read it
  (``acl.authorize_query`` for SPARQL, ``GET /audit`` for the listing).
- Payloads are never copied, only hashed and sized: copying would dual-write
  state and would leak private persona-graph content into a log others may
  read.

What this is not: event sourcing. Writes are state-first, so a graph cannot
be rebuilt from its audit entries.

Failure posture (a deliberate deviation worth stating): a failure to write
an entry is logged loudly and never fails the request. The middleware runs
after the handler, so it cannot make the *data* write conditional on the
*audit* write. Failing closed for identity/delegation/admin changes would
need an intent entry written before the operation; that is not built.

Who may turn it down, and how (CHANGED 2026-09-30 after a live test showed
the first design let an admin silence the log with one SPARQL update):

- The audit *configuration* lives in its own graph, ``urn:{dataset}:audit:config``,
  which gets the same protection as the audit graph itself: nobody can write
  it through the API, admin included, so the only way to change it is the
  audited routes (``POST /audit/level``, ``/audit/suspend``, ``/audit/resume``).
  Before this it was a triple in the ``meta`` graph, which admins can write;
  that triple is now ignored.
- The API has a **floor**: ``POST /audit/level`` accepts only ``mutations``,
  ``standard`` or ``default`` (clear the override). Anything quieter --
  ``denials`` or ``off`` -- is either a **time-boxed suspension**
  (``POST /audit/suspend``: a reason is mandatory, the maximum is
  :data:`MAX_SUSPEND_MINUTES`, it reverts on its own, and both its start and
  its end are recorded) or a **permanent operator decision** made outside the
  API, in the bridge's environment: ``HOLONBRIDGE_AUDIT_LEVEL`` (all
  datasets) and ``HOLONBRIDGE_AUDIT_LEVELS`` (``dataset=level,...``).
- Effective level = an active suspension, else the API-set level, else the
  operator's level. The default is ``standard``.
- Every change made through the API is recorded whatever level it sets, so
  turning audit down never erases the record of doing so.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from . import delegation as dg

logger = logging.getLogger(__name__)

HB = "https://w3id.org/holonbridge/"
XSD = "http://www.w3.org/2001/XMLSchema#"

#: Least to most verbose. See the module docstring.
LEVELS: tuple[str, ...] = ("off", "denials", "mutations", "standard")
DEFAULT_LEVEL = "standard"

#: What ``POST /audit/level`` may set permanently. Anything quieter is a
#: time-boxed suspension or an operator decision.
API_LEVELS: tuple[str, ...] = ("mutations", "standard")

#: What ``POST /audit/suspend`` may suspend audit down to.
SUSPENDABLE: tuple[str, ...] = ("denials", "off")

#: Longest single suspension, in minutes.
MAX_SUSPEND_MINUTES = 240

#: How long a dataset's stored configuration is cached, in seconds.
_CONFIG_TTL = 30.0

#: POST routes that only read, and so are not mutations.
READ_ONLY_POSTS: frozenset[str] = frozenset(
    {"/sparql/select", "/sparql/construct", "/validate"}
)

#: Never recorded (unless denied or a mutation): the audit reader, liveness.
_UNRECORDED_PREFIXES: tuple[str, ...] = ("/audit", "/health")

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_USER_GRAPH = re.compile(r":persona:[^:]+:user:([^:]+):")


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------

def _swap(holons_graph: str, tail: str) -> str:
    suffix = ":holons"
    if not holons_graph.endswith(suffix):
        raise ValueError(f"expected holons_graph to end with {suffix!r}, got {holons_graph!r}")
    return holons_graph[: -len(suffix)] + tail


def audit_graph(holons_graph: str) -> str:
    """``urn:example:holons`` -> ``urn:example:audit`` (bank-scoped form
    follows for free), derived the same way ``acl._admin_role`` and
    ``acl._scratch_area`` derive their org-tier resources."""
    return _swap(holons_graph, ":audit")


def config_graph(holons_graph: str) -> str:
    """The graph holding this dataset's audit configuration. Its IRI is also
    the IRI of the configuration resource inside it."""
    return _swap(holons_graph, ":audit:config")


config_iri = config_graph  # the configuration resource shares its graph's IRI


def is_audit_graph(target: str, holons_graph: str) -> bool:
    """Whether ``target`` is one of the two graphs only the bridge may write
    and only admins may read: the audit log and its configuration."""
    return target in (audit_graph(holons_graph), config_graph(holons_graph))


# --------------------------------------------------------------------------
# Per-request annotation
# --------------------------------------------------------------------------

_current: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "holonbridge_audit", default=None
)


def annotate(**fields: Any) -> None:
    """Attach detail to the current request's audit entry. Recognised keys:
    ``graphs`` (list of IRIs), ``write_mode``, ``authorised_by``,
    ``authorised_reason``, ``payload_hash``, ``payload_bytes``,
    ``security_read`` (bool), ``reason``. ``None`` values are ignored, and
    this is a no-op outside a request, so callers never need to check."""
    ctx = _current.get()
    if ctx is None:
        return
    for key, value in fields.items():
        if value is not None:
            ctx[key] = value


def payload_digest(text: str) -> tuple[str, int]:
    """``("sha256:<hex>", byte_length)`` for a payload."""
    raw = text.encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest(), len(raw)


def foreign_user_graphs(graph_refs: Iterable[str], caller_slug: str) -> list[str]:
    """The persona-user graphs among ``graph_refs`` that belong to somebody
    other than the caller. ``public`` is the persona's shared graph, not a
    person's, so it is not foreign."""
    found = []
    for graph in graph_refs:
        match = _USER_GRAPH.search(graph)
        if match and match.group(1) not in ("public", caller_slug):
            found.append(graph)
    return sorted(found)


# --------------------------------------------------------------------------
# Levels: operator, API and suspension
# --------------------------------------------------------------------------

def default_level() -> str:
    """The process-wide operator default, ``HOLONBRIDGE_AUDIT_LEVEL``."""
    value = os.environ.get("HOLONBRIDGE_AUDIT_LEVEL", DEFAULT_LEVEL).strip().lower()
    return value if value in LEVELS else DEFAULT_LEVEL


def operator_level(dataset: str) -> str:
    """The level the operator has decided for ``dataset``:
    ``HOLONBRIDGE_AUDIT_LEVELS`` (``dataset=level,other=level``) if it names
    the dataset, else :func:`default_level`. Set in the bridge's environment,
    so changing it takes host access and a restart -- not an API call. This
    is the only way to make ``denials`` or ``off`` permanent."""
    for part in os.environ.get("HOLONBRIDGE_AUDIT_LEVELS", "").split(","):
        name, _, level = part.partition("=")
        if name.strip() == dataset and level.strip().lower() in LEVELS:
            return level.strip().lower()
    return default_level()


@dataclass(frozen=True)
class Config:
    """A dataset's stored audit configuration, from its config graph."""

    api_level: str | None = None
    suspended_to: str | None = None
    suspended_until: datetime | None = None
    suspended_reason: str | None = None
    suspended_by: str | None = None

    def suspension_active(self, now: datetime) -> bool:
        return bool(
            self.suspended_to in LEVELS and self.suspended_until and now < self.suspended_until
        )


def _parse_dt(value: str) -> datetime | None:
    """Tolerant xsd:dateTime parse (older Pythons reject a trailing ``Z``)."""
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(re.sub(r"\.\d+", "", text))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_config_cache: dict[tuple[str, str], tuple[float, Config]] = {}
_level_cache = _config_cache  # the name the tests and older callers know


def forget_level(conn) -> None:
    _config_cache.pop((conn.bank_name, conn.dataset), None)


async def _config(client, conn, *, fresh: bool = False) -> Config:
    """The stored configuration, cached briefly. Any failure to read it
    yields an empty :class:`Config` (so the operator's level applies) rather
    than raising."""
    key = (conn.bank_name, conn.dataset)
    hit = _config_cache.get(key)
    now = time.monotonic()
    if not fresh and hit and now - hit[0] < _CONFIG_TTL:
        return hit[1]

    cfg = Config()
    graph = config_graph(conn.holons_graph)
    try:
        result = await client.select(
            conn,
            f"""PREFIX hb: <{HB}>
SELECT ?level ?to ?until ?reason ?by WHERE {{ GRAPH <{graph}> {{
  OPTIONAL {{ <{graph}> hb:auditLevel ?level }}
  OPTIONAL {{ <{graph}> hb:suspendedTo ?to ; hb:suspendedUntil ?until }}
  OPTIONAL {{ <{graph}> hb:suspendedReason ?reason }}
  OPTIONAL {{ <{graph}> hb:suspendedBy ?by }}
}} }} LIMIT 1""",
        )
        rows = result.get("results", {}).get("bindings", [])
        if rows:
            row = rows[0]
            level = row.get("level", {}).get("value")
            cfg = Config(
                api_level=level if level in LEVELS else None,
                suspended_to=row.get("to", {}).get("value"),
                suspended_until=(
                    _parse_dt(row["until"]["value"]) if "until" in row else None
                ),
                suspended_reason=row.get("reason", {}).get("value"),
                suspended_by=row.get("by", {}).get("value"),
            )
    except Exception as exc:  # noqa: BLE001 - never let config lookup break a request
        logger.warning("audit: could not read configuration for %s: %s", conn.dataset, exc)
    _config_cache[key] = (now, cfg)
    return cfg


_end_locks: dict[tuple[str, str], asyncio.Lock] = {}


async def _end_expired_suspension(client, conn) -> None:
    """Record that a suspension ran out, and clear it. Called by whichever
    request first notices the expiry, so the "resumed" entry is stamped when
    it was noticed, not at the scheduled end (there may be no traffic in
    between); its reason states the scheduled end. The lock and re-read stop
    concurrent requests in this process writing it twice; separate
    processes could still, rarely, each write one."""
    key = (conn.bank_name, conn.dataset)
    async with _end_locks.setdefault(key, asyncio.Lock()):
        cfg = await _config(client, conn, fresh=True)
        now = datetime.now(timezone.utc)
        if not cfg.suspended_to or not cfg.suspended_until or now < cfg.suspended_until:
            return
        await record(
            client,
            conn,
            {
                "at": now,
                "outcome": "allowed",
                "operation": "audit.resumed",
                "method": "SYSTEM",
                "path": "/audit/suspend",
                "status": 200,
                "actor": cfg.suspended_by,
                "reason": (
                    f"audit suspension to {cfg.suspended_to} ended at expiry "
                    f"({dg._iso(cfg.suspended_until)})"
                ),
            },
        )
        await _clear_suspension(client, conn)


async def level_for(client, conn) -> str:
    """The effective audit level for ``conn``'s dataset: an active
    suspension, else the API-set level, else the operator's."""
    cfg = await _config(client, conn)
    now = datetime.now(timezone.utc)
    if cfg.suspended_to and cfg.suspended_until and now >= cfg.suspended_until:
        await _end_expired_suspension(client, conn)
        cfg = await _config(client, conn, fresh=True)
    if cfg.suspension_active(now):
        return cfg.suspended_to  # type: ignore[return-value]
    return cfg.api_level or operator_level(conn.dataset)


async def describe(client, conn) -> dict[str, Any]:
    """Everything an admin needs to see about the current setting."""
    forget_level(conn)
    effective = await level_for(client, conn)
    cfg = await _config(client, conn, fresh=True)
    now = datetime.now(timezone.utc)
    suspension = None
    if cfg.suspension_active(now):
        suspension = {
            "level": cfg.suspended_to,
            "until": dg._iso(cfg.suspended_until),  # type: ignore[arg-type]
            "reason": cfg.suspended_reason,
            "by": cfg.suspended_by,
        }
    return {
        "effective": effective,
        "api_level": cfg.api_level,
        "operator_level": operator_level(conn.dataset),
        "suspension": suspension,
        "levels": list(LEVELS),
        "api_settable": list(API_LEVELS) + ["default"],
        "suspendable": list(SUSPENDABLE),
        "max_suspend_minutes": MAX_SUSPEND_MINUTES,
    }


_SUSPENSION_PREDICATES = ("suspendedTo", "suspendedUntil", "suspendedReason", "suspendedBy")


def _delete_suspension_sql(graph: str) -> str:
    return " ;\n".join(
        f"DELETE WHERE {{ GRAPH <{graph}> {{ <{graph}> hb:{p} ?o }} }}"
        for p in _SUSPENSION_PREDICATES
    )


async def _clear_suspension(client, conn) -> None:
    graph = config_graph(conn.holons_graph)
    await client.update(conn, f"PREFIX hb: <{HB}>\n{_delete_suspension_sql(graph)}")
    forget_level(conn)


async def set_level(client, conn, level: str) -> None:
    """Set the API-level override: ``mutations`` or ``standard`` (the API
    floor), or ``default`` to clear it and fall back to the operator's level.
    Raises ``ValueError`` for anything else, ``denials`` and ``off``
    included -- see the module docstring."""
    graph = config_graph(conn.holons_graph)
    if level == "default":
        await client.update(
            conn,
            f"PREFIX hb: <{HB}>\nDELETE WHERE {{ GRAPH <{graph}> {{ <{graph}> hb:auditLevel ?o }} }}",
        )
    elif level in API_LEVELS:
        await client.update(
            conn,
            f"""PREFIX hb: <{HB}>
DELETE WHERE {{ GRAPH <{graph}> {{ <{graph}> hb:auditLevel ?o }} }} ;
INSERT DATA {{ GRAPH <{graph}> {{ <{graph}> hb:auditLevel {dg._literal(level)} }} }}""",
        )
    else:
        raise ValueError(
            f"only {list(API_LEVELS)} or 'default' can be set permanently through the API; "
            "use POST /audit/suspend for a time-boxed 'denials' or 'off', or set "
            "HOLONBRIDGE_AUDIT_LEVEL / HOLONBRIDGE_AUDIT_LEVELS in the bridge's environment"
        )
    forget_level(conn)


async def suspend(
    client, conn, *, level: str, minutes: int, reason: str, by: str | None
) -> datetime:
    """Suspend auditing down to ``level`` for ``minutes``. A reason is
    mandatory. Replaces any suspension already running. Returns the moment
    it lapses. Raises ``ValueError`` on bad input."""
    if level not in SUSPENDABLE:
        raise ValueError(f"a suspension may only lower audit to one of {list(SUSPENDABLE)}")
    if not 1 <= minutes <= MAX_SUSPEND_MINUTES:
        raise ValueError(f"minutes must be between 1 and {MAX_SUSPEND_MINUTES}")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a reason is required to suspend auditing")
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    graph = config_graph(conn.holons_graph)
    actor = _iri(by)
    by_triple = f"<{graph}> hb:suspendedBy <{actor}> ." if actor else ""
    await client.update(
        conn,
        f"""PREFIX hb: <{HB}>
PREFIX xsd: <{XSD}>
{_delete_suspension_sql(graph)} ;
INSERT DATA {{ GRAPH <{graph}> {{
  <{graph}> hb:suspendedTo {dg._literal(level)} ;
            hb:suspendedUntil "{_iso_micro(until)}"^^xsd:dateTime ;
            hb:suspendedReason {dg._literal(reason[:200])} .
  {by_triple}
}} }}""",
    )
    forget_level(conn)
    return until


async def resume(client, conn) -> Config | None:
    """End a running suspension early. Returns what it was, or ``None`` if
    none was running."""
    cfg = await _config(client, conn, fresh=True)
    if not cfg.suspended_to:
        return None
    await _clear_suspension(client, conn)
    return cfg


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def is_mutation(method: str, route_path: str) -> bool:
    return method.upper() in _MUTATING_METHODS and route_path not in READ_ONLY_POSTS


# Statuses that are a refusal only on one particular route. A 409 is not a
# refusal in general (it can be a plain write conflict), but on the claim route
# it means "you asked to own a dataset that already has an owner": a request
# the bridge declined, which belongs in the same record as a 401/403 and must
# survive the ``denials`` level.
_ROUTE_REFUSALS: dict[str, frozenset[int]] = {
    "/admin/dataset/initialise": frozenset({409}),
}


def outcome_for(status_code: int, route_path: str | None = None) -> str:
    if status_code in (401, 403):
        return "denied"
    if route_path is not None and status_code in _ROUTE_REFUSALS.get(route_path, ()):
        return "denied"
    if status_code >= 500:
        return "error"
    return "allowed"


def should_record(level: str, outcome: str, mutation: bool, security_read: bool) -> bool:
    """The whole policy in one place. ``off`` records nothing; ``denials``
    only refusals; ``mutations`` refusals and every mutation (including
    failed ones); ``standard`` adds security reads."""
    if level == "off" or level not in LEVELS:
        return False
    if outcome == "denied":
        return True
    if level == "denials":
        return False
    if mutation:
        return True
    return level == "standard" and security_read and outcome == "allowed"


# --------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------

def _iso_micro(when: datetime) -> str:
    """xsd:dateTime with microseconds, so entries made within the same
    second still order correctly (``delegation._iso`` truncates to seconds,
    which is right for validity windows but not for a log)."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _lit(value: object) -> str:
    return dg._literal(str(value)[:500])


def _iri(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return dg.safe_iri(value)
    except dg.DelegationError:
        return None


def new_entry_iri(holons_graph: str) -> str:
    return f"{audit_graph(holons_graph)}:entry:{uuid.uuid4().hex}"


def build_entry(entry: dict[str, Any]) -> str:
    """Turtle for one audit entry. Everything that came from a request is
    either validated as an IRI (:func:`delegation.safe_iri`) or written as a
    length-capped, escaped literal, so a hostile path or graph name cannot
    change the structure of the statement."""
    props: list[str] = [
        f'hb:at "{_iso_micro(entry["at"])}"^^xsd:dateTime',
        f"hb:outcome {_lit(entry['outcome'])}",
        f"hb:operation {_lit(entry['operation'])}",
        f"hb:method {_lit(entry['method'])}",
        f"hb:path {_lit(entry['path'])}",
        f"hb:status {int(entry['status'])}",
    ]
    actor, real = _iri(entry.get("actor")), _iri(entry.get("real_actor"))
    if actor:
        props.append(f"hb:actor <{actor}>")
    if real and real != actor:
        props.append(f"hb:realActor <{real}>")
    if entry.get("acting_as"):
        props.append('hb:actingAs "true"^^xsd:boolean')
    # What kind of credential authenticated the call. This is the control that
    # tells a BOUND identity ("identity": the token itself named the caller)
    # from an ASSERTED one ("proxy" or "legacy": the caller said who they were
    # in the x-holon-animus-id header). Absent on a request that never got as
    # far as a recognised token (a plain 401).
    if entry.get("token_kind"):
        props.append(f"hb:tokenKind {_lit(entry['token_kind'])}")
    for graph in entry.get("graphs") or []:
        iri = _iri(graph)
        props.append(f"hb:targetGraph <{iri}>" if iri else f"hb:targetGraphText {_lit(graph)}")
    if entry.get("write_mode"):
        props.append(f"hb:writeMode {_lit(entry['write_mode'])}")
    by = _iri(entry.get("authorised_by"))
    if by:
        props.append(f"hb:authorisedBy <{by}>")
    if entry.get("authorised_reason"):
        props.append(f"hb:authorisedReason {_lit(entry['authorised_reason'])}")
    if entry.get("payload_hash"):
        props.append(f"hb:payloadHash {_lit(entry['payload_hash'])}")
    if entry.get("payload_bytes") is not None:
        props.append(f"hb:payloadBytes {int(entry['payload_bytes'])}")
    if entry.get("security_read"):
        props.append('hb:securityRead "true"^^xsd:boolean')
    if entry.get("reason"):
        props.append(f"hb:reason {_lit(entry['reason'])}")

    return (
        f"@prefix hb:  <{HB}> .\n@prefix xsd: <{XSD}> .\n\n"
        f"<{entry['iri']}> a hb:AuditEntry ;\n    "
        + " ;\n    ".join(props)
        + " .\n"
    )


async def record(client, conn, entry: dict[str, Any]) -> bool:
    """Write one entry to the audit graph through the bridge's own client
    (bypassing the API's ACL, which refuses this graph to everyone). Returns
    False and logs, rather than raising, on any failure."""
    try:
        entry.setdefault("iri", new_entry_iri(conn.holons_graph))
        await asyncio.wait_for(
            client.post_graph(conn, audit_graph(conn.holons_graph), build_entry(entry)),
            timeout=5.0,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - see the module docstring's failure posture
        logger.warning("audit: FAILED to record %s: %s", entry.get("operation"), exc)
        return False


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------

class AuditMiddleware(BaseHTTPMiddleware):
    """Records requests per the policy in :func:`should_record`.

    Needs ``request.state.conn`` and (when there is one) ``request.state.animus``,
    which ``deps.require_conn`` / ``deps.require_animus`` set. A request that
    never reached ``require_conn`` (a 404 for an unknown path, say) has no
    dataset to record against and is skipped.
    """

    async def dispatch(self, request: Request, call_next):
        ctx: dict[str, Any] = {}
        token = _current.set(ctx)
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            _current.reset(token)
            await self._maybe_record(request, status_code, ctx)

    async def _maybe_record(self, request: Request, status_code: int, ctx: dict[str, Any]) -> None:
        try:
            conn = getattr(request.state, "conn", None)
            if conn is None:
                return
            route = request.scope.get("route")
            template = getattr(route, "path", None) or request.url.path
            outcome = outcome_for(status_code, template)
            mutation = is_mutation(request.method, template)
            if template.startswith(_UNRECORDED_PREFIXES) and outcome != "denied" and not mutation:
                return

            animus = getattr(request.state, "animus", None)
            acting_as = bool(getattr(animus, "acting_as", False))
            security_read = (
                bool(ctx.get("security_read"))
                or acting_as
                or (template == "/holon" and "on_behalf_of" in request.query_params)
            )

            client = request.app.state.fuseki
            level = await level_for(client, conn)
            # A change to the audit configuration is always recorded, whatever
            # level it sets: otherwise turning audit off would erase the
            # record of turning it off. Initialising a dataset (the first owner
            # being created) is likewise never left to the audit level, and
            # neither is creating a dataset: it allocates storage on the
            # server, and the bridge has no way to take it back.
            forced = (
                mutation
                and outcome == "allowed"
                and (
                    template.startswith("/audit")
                    or template in ("/admin/dataset/initialise", "/admin/dataset/shapes", "/admin/dataset/snapshot", "/admin/dataset/restore", "/datasets")
                )
            )
            if not forced and not should_record(level, outcome, mutation, security_read):
                return

            entry: dict[str, Any] = {
                "at": datetime.now(timezone.utc),
                "outcome": outcome,
                "operation": f"{request.method} {template}",
                "method": request.method,
                "path": request.url.path,
                "status": status_code,
                "actor": getattr(animus, "person", None),
                "real_actor": getattr(animus, "real_person", None),
                "acting_as": acting_as,
                "token_kind": getattr(getattr(request.state, "token_auth", None), "kind", None),
                "security_read": (not mutation) and security_read,
            }
            entry.update({k: v for k, v in ctx.items() if k != "security_read"})
            await record(client, conn, entry)
        except Exception as exc:  # noqa: BLE001
            logger.warning("audit: middleware error: %s", exc)
