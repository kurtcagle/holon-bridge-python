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

CHANGED 2026-08-17: added ``persona``/``personaSource``. whoami already
answers "who am I"; "and under which persona am I reading" is the same
question one layer out -- see persona_state.py for what the source values
mean.

CHANGED 2026-09-01: added ``acting_as``/``real_person``/
``real_person_label``. Same reasoning one layer further out again: while
an admin act_as override is active, ``person`` answers "who is this
request being treated as," which is no longer the same question as "who
actually authenticated" -- see acting_as.py. For an ordinary request
these fields are redundant with person/person_label (real_person always
equals person, acting_as is always False); they only diverge, and only
then matter, mid-impersonation.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..deps import AnimusDep, ConnDep, PersonasDep

router = APIRouter(tags=["identity"])


@router.get("/whoami")
async def whoami(animus: AnimusDep, conn: ConnDep, personas: PersonasDep) -> dict:
    """The resolved Person for the caller's current credential.

    ``person`` is the resolved Person IRI -- the fact worth checking.
    ``external_id`` / ``external_id_type`` echo back what was presented
    (e.g. a GitHub login), so a mismatch between what was sent and what
    resolved is visible directly rather than inferred. ``teams`` is
    whatever Teams the resolved Person belongs to, empty until team
    structure actually exists.

    ``persona`` is this person's active persona override for the current
    dataset (``conn.dataset``), or ``null`` if none is set -- ground truth
    only. ``personaSource`` is ``explicit`` (set by switch_persona this
    process run), ``persisted`` (restored from a prior run), ``env`` (from
    ``HOLONBRIDGE_PERSONA``, only when this person has no stored entry),
    or ``none``.

    ``acting_as`` is True while an admin act_as override is active for
    this credential -- in which case ``person``/``person_label`` describe
    the impersonated target, and ``real_person``/``real_person_label``
    describe who actually authenticated. When no override is active,
    ``real_person``/``real_person_label`` simply equal ``person``/
    ``person_label`` and ``acting_as`` is False.

    Requires nothing beyond identity resolution -- AnimusDep already 401s
    on a missing or unresolvable identity before this handler runs, so a
    response from here always carries a real, resolved Person.
    """
    persona, persona_source = personas.get(person_id=animus.person, dataset=conn.dataset)
    return {
        "person": animus.person,
        "person_label": animus.person_label,
        "external_id": animus.external_id,
        "external_id_type": animus.external_id_type,
        "teams": sorted(animus.teams),
        "persona": persona,
        "personaSource": persona_source,
        "acting_as": animus.acting_as,
        "real_person": animus.real_person,
        "real_person_label": animus.real_person_label,
    }
