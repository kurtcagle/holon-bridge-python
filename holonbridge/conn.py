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

    def graph(self, role: str) -> str:
        """Return the canonical graph IRI for a role in this dataset."""
        if role not in GRAPH_ROLES:
            raise ValueError(
                f"unknown graph role {role!r}; expected one of {', '.join(GRAPH_ROLES)}"
            )
        if self._bank_scoped:
            return f"urn:{self.bank_name}:{self.dataset}:{role}"
        return f"urn:{self.dataset}:{role}"

    def scoped(self, role: str, key: str) -> str:
        """A per-artefact graph under a role: ``urn:{dataset}:{role}:{key}``,
        or ``urn:{bank}:{dataset}:{role}:{key}`` once this dataset has opted
        into the bank-scoped convention.

        Used where one graph per artefact beats one graph holding many —
        a pipeline manifest, for instance, which is far easier to replace,
        drop, and reason about on its own.
        """
        if role not in GRAPH_ROLES:
            raise ValueError(f"unknown graph role {role!r}")
        if not key or any(ch in key for ch in "<> \"{}|^`"):
            raise ValueError(f"{key!r} cannot be used in a graph IRI")
        singular = role[:-1] if role.endswith("s") else role
        if self._bank_scoped:
            return f"urn:{self.bank_name}:{self.dataset}:{singular}:{key}"
        return f"urn:{self.dataset}:{singular}:{key}"

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
