"""Agent endpoints and agent calls: the data, with no I/O.

An **endpoint** (``hb:AgentEndpoint``) is somewhere the bridge may send a task:
a URL, how to authenticate to it, and the limits it is held to. A **call**
(``hb:AgentCall``) is one task sent there: who asked, exactly what was sent,
and what came back.

Statuses of a call::

    PendingApproval --approve--> Approved --send--> Sending --> Responded
          |                         ^                  |
          +--reject--> Rejected     |                  +-----> Failed --retry--+
                                    +--------------------------------------+

``Approved`` is a durable queue position, not a transient step: a call that a
*policy* approved (a trigger registered with ``reviewRequired: false`` against
an endpoint that does not require review) is sent by the scheduler's
``agent-dispatch`` job rather than inside whichever request happened to cause
it. A person approving interactively sends straight away.

A call is sent **at most once per approval**. The status moves to ``Sending``
before any bytes leave, so a crash mid-send leaves ``Sending`` behind rather
than a call that could silently go twice. Sending again is an explicit
``retry``, and the call's id travels as the idempotency key, so an agent that
deduplicates will treat the retry as the same task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

HB = "https://w3id.org/holonbridge/"

ENDPOINT_CLASS_SUFFIX = "AgentEndpoint"
CALL_CLASS_SUFFIX = "AgentCall"

ENDPOINT_STATUSES = ("Active", "Suspended", "Deprecated")
AUTH_SCHEMES = ("none", "bearer", "header")

PENDING = "PendingApproval"
APPROVED = "Approved"
SENDING = "Sending"
RESPONDED = "Responded"
FAILED = "Failed"
REJECTED = "Rejected"
CALL_STATUSES = (PENDING, APPROVED, SENDING, RESPONDED, FAILED, REJECTED)

#: Calls that can still change what leaves the bridge. A trigger does not stack
#: a second call for the same focus while one of these exists.
OPEN_STATUSES = (PENDING, APPROVED, SENDING)

APPROVAL_HUMAN = "human"
APPROVAL_POLICY = "policy"

REQUEST_SCHEMA = "holonbridge.agent-request/1"

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_RESPONSE_BYTES = 1_000_000
HARD_MAX_RESPONSE_BYTES = 10_000_000
DEFAULT_MAX_REQUEST_BYTES = 1_000_000
HARD_MAX_REQUEST_BYTES = 10_000_000

#: Most rows a context query may contribute to a request. A SELECT with no
#: bound would otherwise ship the whole graph to the agent.
MAX_CONTEXT_ROWS = 500

#: A ``Sending`` call this much older than its endpoint's timeout is presumed
#: lost (the process died mid-send) and may be retried.
STALE_SENDING_GRACE_SECONDS = 60.0


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class Endpoint:
    id: str
    iri: str
    url: str
    status: str = "Active"
    label: str = ""
    description: str = ""
    auth_scheme: str = "none"
    #: Header name when ``auth_scheme`` is ``header`` (e.g. ``X-Api-Key``).
    auth_header: str = ""
    #: ``env:NAME``. The secret itself is never stored in the graph, logged, or
    #: echoed in any response; it is read from the process environment only at
    #: the moment of sending.
    credential_ref: str = ""
    #: ``env:NAME`` of the HMAC key used to sign the request body.
    signing_ref: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    #: True: every call to this endpoint needs a person's approval, whatever
    #: registered the trigger or pipeline that asked for it.
    call_review: bool = True
    registered_by: str = ""

    @property
    def active(self) -> bool:
        return self.status == "Active"

    @property
    def has_credentials(self) -> bool:
        return bool(self.credential_ref or self.signing_ref)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "iri": self.iri,
            "label": self.label or self.id,
            "description": self.description,
            "url": self.url,
            "endpointStatus": self.status,
            "authScheme": self.auth_scheme,
            "authHeader": self.auth_header or None,
            # The reference, not the secret: it names an environment variable.
            "credentialRef": self.credential_ref or None,
            "signingRef": self.signing_ref or None,
            "timeoutSeconds": self.timeout_seconds,
            "maxResponseBytes": self.max_response_bytes,
            "maxRequestBytes": self.max_request_bytes,
            "callReview": self.call_review,
            "registeredBy": self.registered_by or None,
        }


@dataclass
class EndpointLoadResult:
    endpoints: list[Endpoint]
    warnings: list[str] = field(default_factory=list)

    def by_id(self, endpoint_id: str) -> Endpoint | None:
        for endpoint in self.endpoints:
            if endpoint.id == endpoint_id:
                return endpoint
        return None


@dataclass
class Call:
    id: str
    iri: str
    endpoint_id: str
    status: str = PENDING
    requested_by: str = ""
    requested_at: str = ""
    task: str = ""
    #: The exact JSON text that is (or was) sent. Stored so that what a person
    #: approves is byte-for-byte what leaves the bridge.
    body: str = ""
    digest: str = ""
    target_graph: str = ""
    focus: str = ""
    via_trigger: str = ""
    via_pipeline: str = ""
    via_stage: str = ""
    approval_mode: str = ""
    approved_by: str = ""
    approved_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    attempts: int = 0
    http_status: int | None = None
    response_summary: str = ""
    response_bytes: int | None = None
    error: str = ""
    candidate: str = ""
    rejected_reason: str = ""
    rejected_by: str = ""

    @property
    def open(self) -> bool:
        return self.status in OPEN_STATUSES

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "iri": self.iri,
            "endpoint": self.endpoint_id,
            "callStatus": self.status,
            "requestedBy": self.requested_by or None,
            "requestedAt": self.requested_at,
            "task": self.task,
            "digest": self.digest,
            "targetGraph": self.target_graph or None,
            "focus": self.focus or None,
            "viaTrigger": self.via_trigger or None,
            "viaPipeline": self.via_pipeline or None,
            "viaStage": self.via_stage or None,
            "approvalMode": self.approval_mode or None,
            "approvedBy": self.approved_by or None,
            "approvedAt": self.approved_at or None,
            "startedAt": self.started_at or None,
            "completedAt": self.completed_at or None,
            "attempts": self.attempts,
            "httpStatus": self.http_status,
            "responseSummary": self.response_summary or None,
            "responseBytes": self.response_bytes,
            "error": self.error or None,
            "candidate": self.candidate or None,
            "rejectedReason": self.rejected_reason or None,
            "rejectedBy": self.rejected_by or None,
        }

    def detail(self) -> dict[str, Any]:
        return {**self.summary(), "request": self.body}
