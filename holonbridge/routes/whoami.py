"""Identity self-check.

One route, one job: tell a caller who the bridge resolved them as. No
permission beyond identity resolution itself is required -- this exists
specifically to answer "which Person did I actually authenticate as"
without needing a grant on anything, which nothing else in the ACL surface
does. Built alongside the admin-role bypass for exactly this reason: every
founder currently holds identical grants, so no permission-based probe can
tell them apart, and the identity-threading work needed something that
could confirm a specific person resolved rather than merely "a person
resolved."
"""

from __future__ import annotations

from fastapi import APIRouter

from ..deps import AnimusDep

router = APIRouter(tags=["identity"])


@router.get("/whoami")
async def whoami(animus: AnimusDep) -> dict:
    """The resolved Person for the caller's current credential.

    ``person`` is the resolved Person IRI -- the fact worth checking.
    ``external_id`` / ``external_id_type`` echo back what was presented
    (e.g. a GitHub login), so a mismatch between what was sent and what
    resolved is visible directly rather than inferred. ``teams`` is
    whatever Teams the resolved Person belongs to, empty until team
    structure actually exists.

    Requires nothing beyond identity resolution -- AnimusDep already 401s
    on a missing or unresolvable identity before this handler runs, so a
    response from here always carries a real, resolved Person.
    """
    return {
        "person": animus.person,
        "person_label": animus.person_label,
        "external_id": animus.external_id,
        "external_id_type": animus.external_id_type,
        "teams": sorted(animus.teams),
    }
