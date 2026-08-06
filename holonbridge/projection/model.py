"""Projection domain model."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .vocab import CHANGE_MODES, DELIVERY_MODES, HOOK_STATUSES


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ProjectionHook:
    """A registered subscriber to some scoped view of the graph."""

    id: str
    iri: str
    target: str = ""
    label: str = ""
    description: str = ""

    # Scope — exactly one of these defines the slice.
    construct: str = ""
    named_query: str = ""

    change_mode: str = "upsert"
    delivery: str = "pull"
    endpoint: str = ""
    key_predicate: str = ""
    media_type: str = "text/turtle"
    status: str = "Active"
    sequence: int = 0

    @property
    def active(self) -> bool:
        return self.status == "Active"

    @property
    def sends_retractions(self) -> bool:
        return self.change_mode != "append"

    @property
    def sends_full_slice(self) -> bool:
        return self.change_mode == "replace"

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "iri": self.iri,
            "label": self.label or self.id,
            "description": self.description,
            "target": self.target,
            "changeMode": self.change_mode,
            "delivery": self.delivery,
            "endpoint": self.endpoint,
            "keyPredicate": self.key_predicate,
            "mediaType": self.media_type,
            "hookStatus": self.status,
            "sequence": self.sequence,
            "scope": "namedQuery" if self.named_query else "construct",
            "namedQuery": self.named_query,
        }

    def detail(self) -> dict[str, Any]:
        return {**self.summary(), "construct": self.construct}

    def problems(self) -> list[str]:
        """Configuration errors that would make this hook unusable."""
        out: list[str] = []
        if not self.target:
            out.append("no target declared")
        if bool(self.construct) == bool(self.named_query):
            out.append("declare exactly one of construct or namedQuery")
        if self.change_mode not in CHANGE_MODES:
            out.append(f"changeMode must be one of {', '.join(CHANGE_MODES)}")
        if self.delivery not in DELIVERY_MODES:
            out.append(f"delivery must be one of {', '.join(DELIVERY_MODES)}")
        if self.delivery == "webhook" and not self.endpoint:
            out.append("a webhook hook needs an endpoint")
        if self.change_mode == "upsert" and not self.key_predicate:
            # Not fatal — a target may key on the subject IRI — but an upsert
            # with no declared key is a decision made by omission.
            out.append(
                "upsert with no keyPredicate: the target will have to key on "
                "the subject IRI"
            )
        if self.status not in HOOK_STATUSES:
            out.append(f"hookStatus must be one of {', '.join(HOOK_STATUSES)}")
        return out


@dataclass
class Envelope:
    """What the bridge hands a target.

    Deliberately just triples plus enough context to interpret them. The
    bridge does not generate SQL, XML, or anything else — that is the target's
    job, and keeping it there is what allows more than one kind of target.
    """

    delivery_id: str
    hook_id: str
    target: str
    dataset: str
    change_mode: str
    sequence: int
    generated_at: str = field(default_factory=stamp)
    key_predicate: str = ""
    media_type: str = "text/turtle"
    additions: str = ""
    retractions: str = ""
    addition_count: int = 0
    retraction_count: int = 0
    full_slice: bool = False

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    @property
    def empty(self) -> bool:
        return self.addition_count == 0 and self.retraction_count == 0

    def as_dict(self, *, include_payload: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {
            "deliveryId": self.delivery_id,
            "hook": self.hook_id,
            "target": self.target,
            "dataset": self.dataset,
            "changeMode": self.change_mode,
            "keyPredicate": self.key_predicate,
            "mediaType": self.media_type,
            "sequence": self.sequence,
            "generatedAt": self.generated_at,
            "fullSlice": self.full_slice,
            "counts": {
                "additions": self.addition_count,
                "retractions": self.retraction_count,
            },
        }
        if include_payload:
            body["additions"] = self.additions
            body["retractions"] = self.retractions
        return body


@dataclass
class Delivery:
    """The record of one attempt to hand an envelope over."""

    id: str
    hook_id: str
    status: str = "pending"
    sequence: int = 0
    created_at: str = field(default_factory=stamp)
    settled_at: str = ""
    attempts: int = 0
    addition_count: int = 0
    retraction_count: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "deliveryId": self.id,
            "hook": self.hook_id,
            "status": self.status,
            "sequence": self.sequence,
            "createdAt": self.created_at,
            "settledAt": self.settled_at,
            "attempts": self.attempts,
            "counts": {
                "additions": self.addition_count,
                "retractions": self.retraction_count,
            },
            "error": self.error,
        }
