"""Per-request connection state.

The Node bridge consolidated a bag of getters into a single ``req.conn``;
this is the same idea. Every handler resolves one ``Conn`` at the top and
passes it down, so dataset overrides can never leak past a route that
forgot to check a header.

The graph naming convention is ``urn:{dataset}:{role}`` -- or, for a
dataset a bank has explicitly opted in via ``bankScopedDatasets``,
``urn:{bank}:{dataset}:{role}``. Keeping it in one place is what stops the
``urn:data:*`` drift that made the SHACL gate unarmable on datasets whose
shapes lived under the wrong prefix; the bank segment is opt-in per
dataset for the same reason -- flipping it unconditionally for every bank
would silently break every dataset that has not actually been rewritten
onto the new convention, which as of 2026-07-29 is every dataset except
worldtest.

CHANGED 2026-08-15: added persona_user_graph / persona_graph /
persona_for_graph. The Aimee/Carlo proof-of-concept needed a graph per
(persona, role, user) -- one level deeper than ``scoped`` goes -- and it
was built by hand against a live dataset before this method existed to
build it correctly. These three methods are that convention made into
code, so nothing constructs ``urn:{dataset}:persona:...`` as a raw string
anywhere else. See the ACL architecture DataBook for why the shape looks
like this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .config import Bank, BankStore, Settings

DATASET_OVERRIDE_HEADER: Final = "x-dataset-override"

#: Canonical per-dataset graph roles.
GRAPH_ROLES: Final = (
    "holons",
    "ontology",
    "events",
    "scene",
    "shacl",
    "named-queries",
    "named-rules",
    "pipelines",
    "messages",
    "projections",
    "projection-log",
    "sequences",
    "meta",
)

_IRI_UNSAFE: Final = "<> \"{}|^`"


@dataclass(frozen=True)
class Conn:
    """Everything a handler needs to reach the backend for this request."""

    base_url: str
    dataset: str
    overridden: bool
    bank_name: str
    token: str | None = None
    bank_scoped_datasets: frozenset[str] = frozenset()

    # --- endpoint construction -------------------------------------------------

    @property
    def query_endpoint(self) -> str:
        return f"{self.base_url}/{self.dataset}/query"

    @property
    def update_endpoint(self) -> str:
        return f"{self.base_url}/{self.dataset}/update"

    @property
    def gsp_endpoint(self) -> str:
        return f"{self.base_url}/{self.dataset}/data"

    @property
    def shacl_endpoint(self) -> str:
        return f"{self.base_url}/{self.dataset}/shacl"

    # --- graph naming ----------------------------------------------------------

    @property
    def _bank_scoped(self) -> bool:
        """Whether this dataset has actually been rewritten onto the
        urn:{bank}:{dataset}:{role} convention, per this bank's own
        bankScopedDatasets list. Never inferred from the graph's contents --
        see the module docstring for why."""
        return self.dataset in self.bank_scoped_datasets

    def _prefix(self) -> str:
        if self._bank_scoped:
            return f"urn:{self.bank_name}:{self.dataset}"
        return f"urn:{self.dataset}"

    def graph(self, role: str) -> str:
        """Return the canonical graph IRI for a role in this dataset."""
        if role not in GRAPH_ROLES:
            raise ValueError(
                f"unknown graph role {role!r}; expected one of {', '.join(GRAPH_ROLES)}"
            )
        return f"{self._prefix()}:{role}"

    def scoped(self, role: str, key: str) -> str:
        """A per-artefact graph under a role: ``urn:{dataset}:{role}:{key}``,
        or ``urn:{bank}:{dataset}:{role}:{key}`` once this dataset has opted
        into the bank-scoped convention.

        Used where one graph per artefact beats one graph holding many --
        a pipeline manifest, for instance, which is far easier to replace,
        drop, and reason about on its own.
        """
        if role not in GRAPH_ROLES:
            raise ValueError(f"unknown graph role {role!r}")
        if not key or any(ch in key for ch in _IRI_UNSAFE):
            raise ValueError(f"{key!r} cannot be used in a graph IRI")
        singular = role[:-1] if role.endswith("s") else role
        return f"{self._prefix()}:{singular}:{key}"

    def persona_user_graph(self, persona: str, role: str, user: str) -> str:
        """Graph IRI for one persona's per-(role, user) scope:
        ``urn:{dataset}:persona:{persona}:user:{user}:{role}``.

        ``user`` is a real person's internal userId, or the reserved literal
        ``"public"`` for that persona's own curated common-knowledge graph
        -- ``public`` is not a real user, see the ACL architecture DataBook.
        ``role`` is one of ``GRAPH_ROLES``, same as everywhere else; in
        practice this is almost always ``"holons"``.
        """
        if role not in GRAPH_ROLES:
            raise ValueError(f"unknown graph role {role!r}")
        for part, label in (("persona", persona), ("user", user)):
            if not label or any(ch in label for ch in _IRI_UNSAFE + ":"):
                raise ValueError(f"{part} {label!r} cannot be used in a graph IRI")
        return f"{self._prefix()}:persona:{persona}:user:{user}:{role}"

    def persona_graph(self, persona: str) -> str:
        """The org-tier ``holon:Persona`` resource IRI itself, e.g.
        ``urn:{dataset}:persona:aimee`` -- distinct from
        ``persona_user_graph``, which names a *graph*, not this holon."""
        if not persona or any(ch in persona for ch in _IRI_UNSAFE + ":"):
            raise ValueError(f"persona {persona!r} cannot be used in a graph IRI")
        return f"{self._prefix()}:persona:{persona}"

    def persona_for_graph(self, graph_iri: str) -> str | None:
        """Inverse of ``persona_user_graph``: given any graph IRI, the
        Persona resource IRI that owns it, or ``None`` if the graph is
        outside the persona/user scheme entirely (org ground truth,
        ``ontology``, ``shacl``, and so on all correctly return ``None`` --
        those are not persona-gated; see the ACL architecture DataBook).
        """
        prefix = f"{self._prefix()}:persona:"
        if not graph_iri.startswith(prefix):
            return None
        remainder = graph_iri[len(prefix):]
        persona, _, _rest = remainder.partition(":")
        if not persona:
            return None
        return self.persona_graph(persona)

    @property
    def holons_graph(self) -> str:
        return self.graph("holons")

    @property
    def ontology_graph(self) -> str:
        return self.graph("ontology")

    @property
    def shapes_graph(self) -> str:
        return self.graph("shacl")

    @property
    def scene_graph(self) -> str:
        return self.graph("scene")

    def describe(self) -> dict[str, object]:
        return {
            "bank": self.bank_name,
            "url": self.base_url,
            "dataset": self.dataset,
            "overridden": self.overridden,
            "graphs": {role: self.graph(role) for role in GRAPH_ROLES},
        }


def resolve_conn(
    *,
    settings: Settings,
    banks: BankStore,
    override: str | None = None,
    bank_name: str | None = None,
) -> Conn:
    """Build the ``Conn`` for one request.

    ``override`` is the ``X-Dataset-Override`` header value: it retargets the
    dataset only, never the server. Anything that refreshes process-global
    state must check ``conn.overridden`` before running.
    """

    bank: Bank = banks.get(bank_name) if bank_name else banks.active

    dataset = bank.dataset
    overridden = False
    if override and settings.allow_dataset_override:
        candidate = override.strip()
        if candidate and candidate != dataset:
            _assert_safe_dataset(candidate)
            dataset = candidate
            overridden = True

    return Conn(
        base_url=bank.url,
        dataset=dataset,
        overridden=overridden,
        bank_name=bank.name,
        token=bank.auth_token,
        bank_scoped_datasets=bank.bank_scoped_datasets,
    )


def _assert_safe_dataset(name: str) -> None:
    """Reject dataset names that could escape the path segment."""
    if not name or len(name) > 128:
        raise ValueError("dataset name must be 1-128 characters")
    if not all(ch.isalnum() or ch in "-_." for ch in name):
        raise ValueError(
            "dataset name may contain only letters, digits, hyphen, underscore, dot"
        )
