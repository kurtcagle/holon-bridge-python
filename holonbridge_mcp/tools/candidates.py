"""Candidate review queue tools.

Where a reviewRequired=true trigger's firing lands: the literal Turtle its
rule would produce, staged for a human to approve or reject rather than
written immediately. Approval always merges (GSP POST) regardless of the
rule's own declared write mode -- an unreviewed action that later gets a
human's approval should never be more destructive than strictly additive,
since the live graph may have changed between proposal and approval.
"""

from __future__ import annotations

from ..session import mcp, _call


@mcp.tool()
async def list_candidates(candidate_status: str | None = None) -> dict:
    """List staged trigger candidates. Filter by Pending, Approved, or Rejected."""
    params = {"candidate_status": candidate_status} if candidate_status else None
    return await _call("GET", "/candidates", params=params)


@mcp.tool()
async def get_candidate(candidate_id: str) -> dict:
    """One candidate's detail, including the literal Turtle it would merge on approval."""
    return await _call("GET", f"/candidate/{candidate_id}")


@mcp.tool()
async def approve_candidate(candidate_id: str) -> dict:
    """Approve a pending candidate: merges its staged Turtle (GSP POST) into
    the target graph, regardless of the underlying rule's own write mode
    (see the module note above). Refused (409) if not Pending."""
    return await _call("POST", f"/candidate/{candidate_id}/approve")


@mcp.tool()
async def reject_candidate(candidate_id: str) -> dict:
    """Reject a pending candidate. No write happens; the candidate is marked
    Rejected. Refused (409) if not Pending."""
    return await _call("POST", f"/candidate/{candidate_id}/reject")
