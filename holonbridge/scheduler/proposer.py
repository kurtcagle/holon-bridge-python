"""Proposers for ``LLMInvocation`` tasks.

A persona returns two things: a Turtle proposal and a one-line summary of what
it did. The summary is for the provenance record and is stripped before the
Turtle is validated — a summary line left in the payload is a parse error, and
a parse error here quarantines a proposal that was actually fine.

Nothing a proposer returns is written straight through. The caller validates
it and quarantines anything that fails, so the worst a bad proposal can do is
land in the quarantine graph with its text intact.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

from ..conn import Conn
from ..fuseki import FusekiClient, FusekiError
from .model import Persona, Task

log = logging.getLogger("holonbridge.scheduler.proposer")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"

_FENCE = re.compile(
    r"```(?:turtle|ttl|turtle12|trig)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE
)
_SUMMARY = re.compile(r"^\s*SUMMARY\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

SYSTEM = """You are a scheduled persona proposing an addition to an RDF knowledge graph.

Return exactly two things:

SUMMARY: one line, plain prose, describing what you are proposing.

Then a single fenced turtle block containing only the triples to add. Declare
every prefix you use. Add nothing you cannot ground in the context supplied —
an empty proposal is a valid answer and is better than an invented one.

Do not include the summary line inside the fenced block."""


class ProposerNotConfigured(RuntimeError):
    """No proposer is available. The firing defers rather than failing."""


class ProposalUnparseable(RuntimeError):
    """The persona replied, but no Turtle could be recovered.

    Carries the raw text so it can be quarantined and looked at, rather than
    disappearing into a log line.
    """

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


class NotConfigured:
    """Default proposer: declines.

    An ``LLMInvocation`` with nothing behind it records ``deferred``. Recording
    ``committed`` for a firing that produced nothing would corrupt both the
    provenance trail and the rate-limit count derived from it.
    """

    async def propose(
        self, conn: Conn, task: Task, persona: Persona | None
    ) -> tuple[str, str]:
        raise ProposerNotConfigured(
            "no proposer is configured for LLMInvocation tasks"
        )


def parse_proposal(text: str) -> tuple[str, str]:
    """Split a reply into (turtle, summary).

    Tolerates a missing fence — a reply that is bare Turtle is accepted if it
    looks like Turtle at all — but never returns the summary line as part of
    the payload.
    """
    summary_match = _SUMMARY.search(text)
    summary = summary_match.group(1).strip() if summary_match else ""

    body = _SUMMARY.sub("", text, count=1) if summary_match else text

    fenced = _FENCE.search(body)
    turtle = fenced.group(1).strip() if fenced else body.strip()

    if not turtle:
        raise ProposalUnparseable("the persona returned nothing to commit", text)

    if not any(
        marker in turtle for marker in ("@prefix", "PREFIX", "<", "a ", ";", ".")
    ):
        raise ProposalUnparseable("no Turtle could be recovered from the reply", text)

    return turtle, summary


class AnthropicProposer:
    """Asks a persona's model for a proposal, grounded in the target graph.

    Context matters more than prompt wording here: a persona shown the shapes
    its output must satisfy and the terms already in use writes something that
    validates. One shown nothing invents a vocabulary and gets quarantined.
    """

    def __init__(
        self,
        client: FusekiClient,
        *,
        api_key: str | None = None,
        default_model: str = DEFAULT_MODEL,
        timeout: float = 120.0,
        max_tokens: int = 2000,
    ) -> None:
        self._client = client
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self._default_model = default_model
        self._timeout = timeout
        self._max_tokens = max_tokens

    async def propose(
        self, conn: Conn, task: Task, persona: Persona | None
    ) -> tuple[str, str]:
        if not self._api_key:
            raise ProposerNotConfigured(
                "ANTHROPIC_API_KEY is not set; LLMInvocation tasks cannot run"
            )

        context = await self._context(conn, task)
        prompt = self._prompt(task, persona, context)

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": (persona.model if persona and persona.model else self._default_model),
                    "max_tokens": self._max_tokens,
                    "system": SYSTEM,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )

        if response.status_code >= 400:
            raise RuntimeError(
                f"proposal request failed: {response.status_code} "
                f"{response.text.strip()[:300]}"
            )

        blocks = response.json().get("content", [])
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        return parse_proposal(text)

    # --- grounding ------------------------------------------------------------

    async def _context(self, conn: Conn, task: Task) -> dict[str, Any]:
        """What is already there, and what the output has to satisfy."""
        shapes = ""
        terms: list[str] = []
        try:
            shapes = await self._client.get_graph(conn, conn.shapes_graph)
        except FusekiError:
            log.warning("could not read shapes for %s", conn.shapes_graph)

        if task.target_graph:
            try:
                results = await self._client.select(
                    conn,
                    f"""SELECT DISTINCT ?p (COUNT(*) AS ?n)
WHERE {{ GRAPH <{task.target_graph}> {{ ?s ?p ?o }} }}
GROUP BY ?p ORDER BY DESC(?n) LIMIT 40""",
                )
                terms = [
                    r["p"]["value"]
                    for r in results.get("results", {}).get("bindings", [])
                ]
            except FusekiError:
                log.warning("could not sample terms in %s", task.target_graph)

        return {"shapes": shapes[:12000], "terms": terms}

    def _prompt(
        self, task: Task, persona: Persona | None, context: dict[str, Any]
    ) -> str:
        parts = [
            f"Dataset: {task.dataset_scope or 'default'}",
            f"Target graph: <{task.target_graph}>",
        ]
        if persona:
            parts.append(f"You are {persona.label or persona.id}.")
        if task.description:
            parts.append(f"Task: {task.description}")
        elif task.label:
            parts.append(f"Task: {task.label}")
        if task.invocation_command:
            parts.append(f"Command: {task.invocation_command}")

        if context["terms"]:
            parts.append(
                "Predicates already in use in the target graph:\n"
                + "\n".join(f"  <{t}>" for t in context["terms"])
            )
        if context["shapes"]:
            parts.append(
                "SHACL shapes your proposal must satisfy:\n" + context["shapes"]
            )

        return "\n\n".join(parts)
