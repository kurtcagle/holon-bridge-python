"""Agent gateway: a registry of external agents, and a gated, ledgered way to
hand them work.

The bridge does not run agents and does not decide what they do. It decides
*whether and what it may send*, records everything it did send, and routes
whatever comes back into the review queue instead of into the graph.

* ``registry`` -- ``hb:AgentEndpoint``: where, how to authenticate (by
  reference to an environment variable, never by value), and the limits.
* ``ledger`` -- ``hb:AgentCall``: one node per task sent, holding the exact
  request body, who asked, who approved, and the outcome.
* ``service`` -- the lifecycle: request, approve, send, settle, retry, dispatch.
* ``sender`` -- the one HTTP implementation, and the one place a credential is
  read.
* ``policy`` -- egress and credential rules.

A call can be placed from a route or MCP tool, from a named trigger
(``hev:onFireAgent``), or from a pipeline ``agent`` stage. Folding calls into
the scheduler's capability and ODRL gates, and into a unified audit view, is a
separate step; see the README.
"""

from .model import (  # noqa: F401
    APPROVED,
    CALL_STATUSES,
    FAILED,
    PENDING,
    REJECTED,
    RESPONDED,
    SENDING,
    Call,
    Endpoint,
    EndpointLoadResult,
)
from .registry import InvalidEndpoint, load_endpoints  # noqa: F401
