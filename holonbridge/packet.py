"""Packets: the receipt a DataBook leaves behind when it is ingested.

Until now ``/ingest`` read two things from a DataBook -- ``graph.named_graph``
and its first RDF block -- landed the block, and dropped the rest. Nothing of
the document's header survived, so nothing in the graph could say "a packet of
this profile has arrived" and nothing could react to one. Processing was
whatever the *caller* put in the request (``pipeline``, ``params``).

This module changes that without adding a second ingest path. After the payload
has landed through :func:`holonbridge.ingest.write_turtle_to_graph` (so the ACL,
SHACL and write-lock behaviour is exactly what it was), the bridge writes one
``hb:Packet`` node into the dataset's ``inbox`` graph and calls the post-write
hook. A named trigger whose condition is "a packet of profile X is ``Received``"
then dispatches it, exactly as any state trigger already does. Which handler
runs is therefore data in the graph (a trigger, a named rule or agent
endpoint), not code in ``/ingest``.

**Facts versus claims.** Everything the bridge asserts about the *receipt* is a
fact it observed: when it arrived, who sent it (the authenticated
``Animus``, never a header field), the payload's hash, which graph it landed in.
Everything the *document* says about itself is a claim, and is recorded as one:
``hb:declaredId``, ``hb:declaredType``, ``hb:declaresProfile``. A trigger
decides whether to honour a claim; nothing here does. In particular the header
never names a handler. A sender chooses *what kind of thing it is sending*; the
receiving graph chooses what happens to that kind of thing.

**The header's own RDF is quarantined.** A header that carries a JSON-LD
``@context`` is lifted to RDF with the same safe path every other JSON-LD input
uses (:mod:`holonbridge.jsonld`: remote contexts refused, named graphs refused).
Those triples are authored by the sender, so they go into a per-packet graph
(``urn:{dataset}:inbox:{id}:header``), never into the ``inbox`` graph. If they
shared it, a header could declare a node of type ``hb:Packet`` with status
``Received`` and fire any handler it liked. A lift that fails does not fail the
ingest -- the payload has already landed -- it is recorded on the packet
(``hb:headerLift``).

**Duplicates.** A packet whose declared id and payload hash match an earlier,
non-duplicate packet is recorded with status ``Duplicate`` and is not
dispatched. Re-sending the same document therefore does not re-run its handler.
A rule firing that fails writes no ledger entry, so the next evaluation tries
that packet again; retrying is the trigger machinery's job, not a reason to
re-send.

**The inbox is a receipt log, not a work queue.** The bridge writes ``Received``
or ``Duplicate`` and never changes it. What a handler did is recorded where the
handler works: the trigger's firing ledger (``urn:{dataset}:trigger-log``, one
entry per trigger and packet, which is also what makes a packet fire once), the
handler's own output graph (derived triples should say ``prov:wasDerivedFrom``
the packet), and, for an agent, its call ledger. A named rule is a CONSTRUCT
with one target graph, so it could not flip a status triple in the inbox even if
it were meant to.

What this does not do: it does not decide what a profile *means*, translate
between profiles, or run anything itself. See
``docs/architecture/packet-ingestion.databook.md``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import audit
from .conn import Conn
from .fuseki import FusekiClient
from .messages import HB, XSD_DATETIME, XSD_INTEGER
from .touched import turtle_predicates
from .trigger_hook import after_write
from .turtle import TurtleSyntaxError, literal

#: The only two statuses the bridge writes, and it never changes one afterwards.
RECEIVED = "Received"
DUPLICATE = "Duplicate"
PACKET_STATUSES = (RECEIVED, DUPLICATE)

PACKET_CLASS = f"{HB}Packet"

#: A scheme, then nothing that terminates or confuses an IRI. Same rule the
#: trigger registry applies before it writes an IRI into Turtle.
_SAFE_IRI = re.compile(r'^[A-Za-z][A-Za-z0-9+.\-]*:[^\s<>"{}|\\^`]*$')


@dataclass(frozen=True)
class Claims:
    """What the document says about itself. Claims, not facts."""

    document_id: str | None = None
    document_type: str | None = None
    profiles: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass
class Packet:
    id: str
    iri: str
    inbox_graph: str
    status: str
    payload_graph: str
    payload_hash: str
    payload_bytes: int
    received_at: str
    received_from: str | None
    claims: Claims
    header_graph: str | None = None
    header_lift: str = "none"
    turtle: str = field(default="", repr=False)
    header_turtle: str | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "iri": self.iri,
            "status": self.status,
            "inboxGraph": self.inbox_graph,
            "payloadGraph": self.payload_graph,
            "payloadHash": self.payload_hash,
            "headerLift": self.header_lift,
        }
        if self.claims.document_id:
            out["declaredId"] = self.claims.document_id
        if self.claims.document_type:
            out["declaredType"] = self.claims.document_type
        if self.claims.profiles:
            out["declaresProfile"] = list(self.claims.profiles)
        if self.header_graph:
            out["headerGraph"] = self.header_graph
        if self.claims.notes:
            out["notes"] = list(self.claims.notes)
        return out


# --- reading the header -------------------------------------------------------


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None


def read_claims(frontmatter: Mapping[str, Any]) -> Claims:
    """The ``id``, ``type`` and ``profiles`` a DataBook header claims.

    ``profiles`` entries may be plain IRIs or mappings carrying ``id``/``iri``/
    ``@id``. An entry that is not a usable absolute IRI is dropped and noted,
    never written: a claim that cannot be written as an IRI cannot be matched
    by a trigger either.
    """
    notes: list[str] = []
    profiles: list[str] = []
    raw = frontmatter.get("profiles")
    if raw is not None and not isinstance(raw, list):
        raw = [raw]
    for entry in raw or []:
        if isinstance(entry, Mapping):
            entry = entry.get("id") or entry.get("iri") or entry.get("@id")
        value = _text(entry)
        if value and _SAFE_IRI.match(value):
            if value not in profiles:
                profiles.append(value)
        else:
            notes.append(f"profile claim {entry!r} is not an absolute IRI and was not recorded")
    return Claims(
        document_id=_text(frontmatter.get("id")),
        document_type=_text(frontmatter.get("type")),
        profiles=tuple(profiles),
        notes=tuple(notes),
    )


def lift_header(frontmatter: Mapping[str, Any]) -> tuple[str | None, str]:
    """Lift a header that carries a JSON-LD ``@context`` to Turtle.

    Returns ``(turtle, outcome)``. ``turtle`` is ``None`` when there is nothing
    to lift or the lift failed; ``outcome`` says which (``"none"``,
    ``"lifted"`` or ``"not lifted: <reason>"``). Never raises for a bad header:
    the payload has already landed by the time this runs.
    """
    if "@context" not in frontmatter:
        return None, "none"
    from .jsonld import from_json_ld_local

    try:
        text = json.dumps(dict(frontmatter), default=str)
        return from_json_ld_local(text), "lifted"
    except (TurtleSyntaxError, TypeError, ValueError) as exc:
        return None, f"not lifted: {exc}"


# --- building the receipt -----------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def packet_iri(conn: Conn, packet_id: str) -> str:
    return conn.scoped("inbox", packet_id)


def header_graph_iri(conn: Conn, packet_id: str) -> str:
    return conn.scoped("inbox", f"{packet_id}:header")


def build_envelope(
    *,
    conn: Conn,
    packet_id: str,
    status: str,
    claims: Claims,
    payload_graph: str,
    payload_hash: str,
    payload_bytes: int,
    received_from: str | None,
    message_id: str,
    header_graph: str | None,
    header_lift: str,
    received_at: str | None = None,
) -> str:
    """The ``hb:Packet`` node, as Turtle for the ``inbox`` graph."""
    iri = packet_iri(conn, packet_id)
    lines = [
        f"<{iri}> a <{PACKET_CLASS}> ;",
        f"  <{HB}packetId> {literal(packet_id)} ;",
        f"  <{HB}packetStatus> {literal(status)} ;",
        f"  <{HB}receivedAt> {literal(received_at or _now(), datatype=XSD_DATETIME)} ;",
        f"  <{HB}payloadGraph> <{payload_graph}> ;",
        f"  <{HB}payloadHash> {literal(payload_hash)} ;",
        f"  <{HB}payloadBytes> {literal(str(payload_bytes), datatype=XSD_INTEGER)} ;",
        f"  <{HB}ingestMessage> <{conn.scoped('messages', message_id)}> ;",
        f"  <{HB}headerLift> {literal(header_lift)} ;",
    ]
    if received_from and _SAFE_IRI.match(received_from):
        lines.append(f"  <{HB}receivedFrom> <{received_from}> ;")
    if claims.document_id:
        lines.append(f"  <{HB}declaredId> {literal(claims.document_id)} ;")
    if claims.document_type:
        lines.append(f"  <{HB}declaredType> {literal(claims.document_type)} ;")
    for profile in claims.profiles:
        lines.append(f"  <{HB}declaresProfile> <{profile}> ;")
    if header_graph:
        lines.append(f"  <{HB}headerGraph> <{header_graph}> ;")
    lines[-1] = lines[-1].rstrip(" ;") + " ."
    return "\n".join(lines) + "\n"


async def is_duplicate(
    client: FusekiClient, conn: Conn, *, document_id: str | None, payload_hash: str
) -> bool:
    """True if an earlier non-duplicate packet has this declared id and hash.

    A document with no declared id has no stable identity to compare, so it is
    never a duplicate.
    """
    if not document_id:
        return False
    result = await client.select(
        conn,
        f"""ASK {{
  GRAPH <{conn.graph("inbox")}> {{
    ?p a <{PACKET_CLASS}> ;
       <{HB}declaredId> {literal(document_id)} ;
       <{HB}payloadHash> {literal(payload_hash)} ;
       <{HB}packetStatus> ?s .
    FILTER (?s != {literal(DUPLICATE)})
  }}
}}""",
    )
    return bool(result.get("boolean"))


async def record_packet(
    *,
    client: FusekiClient,
    conn: Conn,
    frontmatter: Mapping[str, Any],
    payload: str,
    payload_graph: str,
    packet_id: str,
    received_from: str | None,
) -> Packet:
    """Write the receipt for an ingested DataBook and let triggers see it.

    Call this **after** the payload has landed, so a handler that fires on the
    packet finds the payload already in place. ``packet_id`` is the ingest
    ``hb:Message`` id, which makes the two records one pair.

    Raises :class:`~holonbridge.fuseki.FusekiError` if the receipt cannot be
    written. Failures while *evaluating* triggers never escape: the post-write
    hook logs and swallows them, as it does for every other write.
    """
    claims = read_claims(frontmatter)
    digest, size = audit.payload_digest(payload)
    received_at = _now()

    status = (
        DUPLICATE
        if await is_duplicate(client, conn, document_id=claims.document_id, payload_hash=digest)
        else RECEIVED
    )

    header_turtle, header_lift = lift_header(frontmatter)
    header_graph = header_graph_iri(conn, packet_id) if header_turtle else None

    envelope = build_envelope(
        conn=conn,
        packet_id=packet_id,
        status=status,
        claims=claims,
        payload_graph=payload_graph,
        payload_hash=digest,
        payload_bytes=size,
        received_from=received_from,
        message_id=packet_id,
        header_graph=header_graph,
        header_lift=header_lift,
        received_at=received_at,
    )

    inbox = conn.graph("inbox")
    if header_turtle and header_graph:
        # Sender-authored triples first, in their own graph, so the receipt that
        # points at them never refers to something that is not there.
        await client.post_graph(conn, header_graph, header_turtle)
    await client.post_graph(conn, inbox, envelope)

    packet = Packet(
        id=packet_id,
        iri=packet_iri(conn, packet_id),
        inbox_graph=inbox,
        status=status,
        payload_graph=payload_graph,
        payload_hash=digest,
        payload_bytes=size,
        received_at=received_at,
        received_from=received_from,
        claims=claims,
        header_graph=header_graph,
        header_lift=header_lift,
        turtle=envelope,
        header_turtle=header_turtle,
    )

    if status == RECEIVED:
        await after_write(
            client,
            conn,
            graphs=[inbox],
            touched=lambda: turtle_predicates(envelope),
            source=f"packet <{packet.iri}>",
        )
    return packet
