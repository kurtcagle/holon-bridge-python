"""In-memory admin-impersonation state -- the ``act_as``/``cease_acting_as``
testing surface.

Exists to exercise the real (non-bypassed) grant-check code paths in
acl.py without needing a non-admin caller's own credential in hand. Every
check in acl.py (``check_read``, ``check_write``, ``check_replace``,
``check_invoke``) re-derives its own admin bypass by calling ``is_admin``
fresh on whatever ``person`` it's handed -- so substituting *which*
Person a request resolves as, at the ``require_animus`` layer, is enough
on its own to route a request through the real grant logic. Nothing in
this module or in acl.py needs a separate "bypass off" flag; swapping
``Animus.person`` to the target already does it, because the target
(almost certainly) isn't an admin.

Deliberately NOT persisted to disk, unlike PersonaStore (persona_state.py)
or the MCP layer's dataset/bank overrides. Those exist so a deliberate
operator choice survives a restart; an active impersonation is the
opposite kind of state -- a restart should always come back to "acting as
no one," never silently resume an old one. Held on ``app.state.acting_as``,
same lifetime and one-instance-per-process shape as ``app.state.personas``.

Keyed by the REAL admin's own Person IRI, never by the target. Two
different admins impersonating two different targets must not collide,
and one admin's override must never be visible to, or clearable by,
another caller. See ``deps.py``'s ``require_animus`` for how the real
identity is always resolved fresh, ignoring any currently-active
override, before this store is even consulted -- that's what stops an
admin from chaining through their own impersonation to reach a target
they couldn't reach directly as themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

#: How long an act_as override survives without being renewed or cleared.
#: A forgotten override should lapse on its own well within a working
#: session, not linger until someone notices they're still impersonating
#: a co-founder three hours later.
DEFAULT_TTL_SECONDS = 1800  # 30 minutes


@dataclass(frozen=True)
class ActingAsEntry:
    target_person: str
    since: datetime


class ActingAsStore:
    """One instance per process, held on ``app.state.acting_as``."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._by_real_person: dict[str, ActingAsEntry] = {}

    def get(self, *, real_person: str) -> str | None:
        """The Person ``real_person`` is currently acting as, or ``None``.

        Expires silently past the TTL: this returns ``None`` and drops
        the stale entry rather than requiring a caller to separately
        check staleness.
        """
        entry = self._by_real_person.get(real_person)
        if entry is None:
            return None
        if datetime.now(timezone.utc) - entry.since > self._ttl:
            del self._by_real_person[real_person]
            return None
        return entry.target_person

    def set(self, *, real_person: str, target_person: str) -> None:
        """Start (or renew, resetting the TTL clock) ``real_person``
        acting as ``target_person``. Never touches any other real
        person's entry."""
        self._by_real_person[real_person] = ActingAsEntry(
            target_person=target_person, since=datetime.now(timezone.utc)
        )

    def clear(self, *, real_person: str) -> None:
        """Drop ``real_person``'s override, if any. Safe to call with
        none active."""
        self._by_real_person.pop(real_person, None)
