"""Per-person persona session state.

Structural analogue of the MCP layer's dataset/bank override
(holonbridge_mcp/server.py's `_dataset_override` / `_bank_override`), but
it can't live there: those overrides are process-global because "which
backend/dataset" isn't a per-caller question for a single-operator MCP
process forwarding one login. "Which persona is this person currently
reading as" *is* per-caller -- two credentials reaching this same bridge
process each carry their own persona, and neither may move the other (see
test_identity_threading.py -- the remote transport already carries more
than one real login through one process, e.g. kurtcagle alongside
ctownley-cs, so this isn't a hypothetical). Only the REST bridge has the
identity to key that by: it resolves Animus.person per request via
require_animus; the MCP layer never sees more than a login name it
forwards in a header.

Persisted, write-through, same reasoning as switch_dataset/switch_bank in
the MCP layer: a crash gets no chance to run cleanup, so the file must
already be correct going in, not correct-after-the-next-successful-write.

HOLONBRIDGE_PERSONA inverts the usual env-vs-persisted precedence on
purpose. For bank/dataset, an explicit env var always wins over a
persisted override -- those are process-wide pins, and an env var is a
deliberate one. Here, a person's own persisted choice always wins over
the env var instead: the env var is only a *default* for a person with no
stored entry yet, never an override of one they already made. Worth
restating in switch_persona's own docstring, since it's the one place
this differs from the sibling tools' pattern.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Literal

log = logging.getLogger(__name__)

OverrideSource = Literal["env", "persisted", "explicit", "none"]

_DEFAULT_STATE_FILE: Final = Path.home() / ".holonbridge" / "persona-state.json"


class PersonaStore:
    """Loads once at construction, writes through on every change. One
    instance per process, held on ``app.state.personas`` (see
    server.py's lifespan) -- shared across requests the same way
    ``app.state.fuseki`` is."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or Path(
            os.getenv("HOLONBRIDGE_PERSONA_STATE_FILE", "").strip()
            or str(_DEFAULT_STATE_FILE)
        )
        self._by_person: dict[str, dict[str, str]] = self._load()
        # Entries set via .set() during this process's own lifetime, so
        # .get() can report "explicit" instead of "persisted" for them --
        # same distinction the MCP layer's dataset/bank overrides make.
        self._touched: set[tuple[str, str]] = set()

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        by_person = raw.get("personaByPerson", {})
        if not isinstance(by_person, dict):
            return {}
        return {k: dict(v) for k, v in by_person.items()}

    def _save(self) -> None:
        """Best-effort: a failed write must not stop the switch taking
        effect for the rest of this process, only from surviving a
        restart."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "personaByPerson": self._by_person,
                "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning(
                "could not persist persona state to %s: %s (the switch is "
                "still in effect for this process, but will not survive a "
                "restart)",
                self._path,
                exc,
            )

    def get(self, *, person_id: str, dataset: str) -> tuple[str | None, OverrideSource]:
        """This person's active persona for this dataset, and where it
        came from. A persisted entry always wins over HOLONBRIDGE_PERSONA
        -- see the module docstring for why that's the opposite of
        switch_dataset/switch_bank's precedence."""
        persisted = self._by_person.get(person_id, {}).get(dataset)
        if persisted:
            source: OverrideSource = (
                "explicit" if (person_id, dataset) in self._touched else "persisted"
            )
            return persisted, source
        env_default = os.getenv("HOLONBRIDGE_PERSONA", "").strip()
        if env_default:
            return env_default, "env"
        return None, "none"

    def set(self, *, person_id: str, dataset: str, persona: str) -> None:
        """Set (persona truthy) or clear (persona falsy) this person's
        override for this dataset. Never touches any other person's
        entry, and never touches this same person's entry for a different
        dataset."""
        entry = self._by_person.setdefault(person_id, {})
        if persona:
            entry[dataset] = persona
        else:
            entry.pop(dataset, None)
            if not entry:
                self._by_person.pop(person_id, None)
        self._touched.add((person_id, dataset))
        self._save()
