"""Sequence minting.

Counters live in ``urn:{dataset}:sequences``, one resource per named
sequence. Minting is a compare-and-set: the UPDATE only fires if the counter
still holds the value we read, so two concurrent minters cannot both claim
the same number. A losing minter retries.

The counter graph follows the dataset naming convention, which is exactly
what the ``urn:data:*`` datasets got wrong — a mismatched counter graph
silently mints from zero every time.

Each successful mint also writes a MintedIdentifier provenance record,
keyed by a bank+dataset-scoped IRI (``urn:{bank}:{dataset}:id-{identifier}``)
rather than the counter's own IRI -- the counter tracks "what's the next
number," the record tracks "what was this number actually used for, and
who said so." Gap-detection depends on this record existing for every
mint that gets consumed, not on the counter alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .conn import Conn
from .fuseki import FusekiClient
from .turtle import escape_literal

SEQ_NS = "https://w3id.org/holon/sequence/"
MINT_NS = "https://w3id.org/holon/mint#"

_PREFIXES = f"""
PREFIX seq:   <{SEQ_NS}>
PREFIX hmint: <{MINT_NS}>
PREFIX xsd:   <http://www.w3.org/2001/XMLSchema#>
PREFIX rdfs:  <http://www.w3.org/2000/01/rdf-schema#>
"""


class SequenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class MintedId:
    sequence: str
    value: int
    identifier: str
    iri: str


async def mint(
    client: FusekiClient,
    conn: Conn,
    *,
    name: str,
    purpose: str,
    authorised_by: str | None = None,
    prefix: str | None = None,
    pad: int = 4,
    attempts: int = 8,
) -> MintedId:
    """Mint the next identifier in a named sequence.

    ``name`` is the sequence key (for example ``FI`` or ``event``).
    ``prefix`` defaults to ``name`` and is what appears in the identifier.
    ``purpose`` is required -- it's what makes the MintedIdentifier record
    legible on its own, independent of whatever consumed the id.
    ``authorised_by`` is an IRI naming the actor or process responsible;
    omitted if not supplied, though every call site should eventually pass
    one once actor identity exists end-to-end.
    """

    graph = conn.graph("sequences")
    counter = f"{SEQ_NS}{name}"
    label = prefix if prefix is not None else name

    for _ in range(attempts):
        current = await _read(client, conn, graph=graph, counter=counter)
        nxt = current + 1

        update = f"""{_PREFIXES}
DELETE {{
  GRAPH <{graph}> {{ <{counter}> seq:value ?old . }}
}}
INSERT {{
  GRAPH <{graph}> {{
    <{counter}> a seq:Sequence ;
                rdfs:label "{escape_literal(name)}" ;
                seq:prefix "{escape_literal(label)}" ;
                seq:pad {pad} ;
                seq:value "{nxt}"^^xsd:integer .
  }}
}}
WHERE {{
  OPTIONAL {{ GRAPH <{graph}> {{ <{counter}> seq:value ?old . }} }}
  FILTER ( !BOUND(?old) || ?old = "{current}"^^xsd:integer )
}}"""
        await client.update(conn, update)

        confirmed = await _read(client, conn, graph=graph, counter=counter)
        if confirmed == nxt:
            identifier = f"{label}-{nxt:0{pad}d}" if label else f"{nxt:0{pad}d}"
            iri = f"urn:{conn.bank_name}:{conn.dataset}:id-{identifier}"
            authorised_clause = (
                f" ;\n            hmint:authorisedBy <{authorised_by}>"
                if authorised_by
                else ""
            )

            record = f"""{_PREFIXES}
INSERT DATA {{
  GRAPH <{graph}> {{
    <{iri}> a hmint:MintedIdentifier ;
            hmint:hasIdentifier "{escape_literal(identifier)}"^^xsd:string ;
            hmint:hasSequence <{counter}> ;
            hmint:hasPurpose "{escape_literal(purpose)}" ;
            hmint:hasGenerationDate "{datetime.now(timezone.utc).isoformat()}"^^xsd:dateTime{authorised_clause} .
  }}
}}"""
            await client.update(conn, record)

            return MintedId(sequence=name, value=nxt, identifier=identifier, iri=iri)

    raise SequenceError(
        f"could not mint from sequence {name!r} after {attempts} attempts "
        "(contention or a counter being written elsewhere)"
    )


async def peek(client: FusekiClient, conn: Conn, *, name: str) -> int:
    """Current value of a sequence without advancing it."""
    return await _read(
        client, conn, graph=conn.graph("sequences"), counter=f"{SEQ_NS}{name}"
    )


async def _read(
    client: FusekiClient, conn: Conn, *, graph: str, counter: str
) -> int:
    query = f"""{_PREFIXES}
SELECT ?value WHERE {{
  GRAPH <{graph}> {{ <{counter}> seq:value ?value . }}
}} LIMIT 1"""
    results = await client.select(conn, query)
    bindings = results.get("results", {}).get("bindings", [])
    if not bindings:
        return 0
    return int(bindings[0]["value"]["value"])
