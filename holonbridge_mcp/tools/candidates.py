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
async def propose_candidate(
    pipeline_id: str,
    stage_id: str,
    message_id: str,
    turtle: str,
    target_graph: str | None = None,
) -> dict:
    """Stage Turtle produced outside the bridge (a rendered llm-stage
    prompt's completion, an external tool, a CNL parse) as a reviewable
    candidate tied to one pipeline run. The bridge never calls a model
    itself -- this is the handoff point. Never writes anything by itself;
    the actual merge only happens later, on approve_candidate, gated the
    same way as any other write."""
    return await _call(
        "POST",
        f"/pipeline/{pipeline_id}/propose-candidate",
        json_body={
            "stage": stage_id,
            "message_id": message_id,
            "turtle": turtle,
            "target_graph": target_graph,
        },
    )


@mcp.tool()
async def approve_candidate(candidate_id: str, reason: str | None = None) -> dict:
    """Approve a pending candidate: merges its staged Turtle (GSP POST) into
    the target graph, regardless of the underlying rule's own write mode
    (see the module note above). Refused (409) if not Pending. The candidate
    keeps who approved it, when, and the optional ``reason`` (up to 1000
    characters)."""
    return await _call(
        "POST",
        f"/candidate/{candidate_id}/approve",
        json_body={"reason": reason} if reason else None,
    )


@mcp.tool()
async def reject_candidate(candidate_id: str, reason: str | None = None) -> dict:
    """Reject a pending candidate. No write happens; the candidate is marked
    Rejected and keeps who rejected it, when, and the optional ``reason`` (up
    to 1000 characters). Refused (409) if not Pending."""
    return await _call(
        "POST",
        f"/candidate/{candidate_id}/reject",
        json_body={"reason": reason} if reason else None,
    )
