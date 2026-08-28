"""Sequence-minting tool."""

from __future__ import annotations

from ..session import mcp, _call


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
