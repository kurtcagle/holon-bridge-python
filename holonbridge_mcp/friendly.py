"""Plain-language messages for errors the bridge returns to MCP callers.

``session._call`` hands a refused request back as ``{"error": True, "status": ...,
"detail": ...}``. That is exact but reads as a fault to someone who is not an
engineer: a 403 from the grant model, an expired sign-in and a malformed query
all look alike. :func:`plain_message` turns the status, the request path and the
bridge's own ``detail`` into one sentence saying what happened and what to do.

It only adds a ``message``; ``status`` and ``detail`` are unchanged, so callers
that read them keep working. It never raises: an unrecognised error gets the
generic sentence for its status class, and a status it has nothing to say about
gets ``None`` (the caller then adds no message).

The substring checks match wording in ``holonbridge/deps.py``, ``acl.py`` and the
routes. They are deliberately loose; if the bridge's wording changes, the status
fallback still applies and the test that pins each case names the phrase.
"""

from __future__ import annotations

from typing import Any


def _text(detail: Any) -> str:
    """The detail as one lower-case string, whatever shape the bridge sent."""
    if isinstance(detail, str):
        return detail.lower()
    if isinstance(detail, dict):
        parts = [str(v) for v in detail.values()]
        return " ".join(parts).lower()
    if isinstance(detail, list):
        return " ".join(str(v) for v in detail).lower()
    return str(detail or "").lower()


def _validation_path(path: str) -> bool:
    return path.rstrip("/").endswith("/validate")


def plain_message(status: int, path: str, detail: Any) -> str | None:
    """One sentence for a person, or ``None`` when there is nothing useful to add."""
    d = _text(detail)

    if status == 401:
        if "no person found" in d:
            return (
                "You are signed in, but this dataset has no account for you yet. "
                "Ask the dataset owner to add you."
            )
        return (
            "Your sign-in is missing, expired or not recognised. "
            "Disconnect the connector and connect it again to sign in."
        )

    if status == 403:
        if "different identity" in d:
            return (
                "This credential belongs to another user, so the request was refused. "
                "Use your own sign-in."
            )
        if "graph" in d and ("name each graph" in d or "reaches graphs it does not name" in d):
            return (
                "This query reads graphs without naming them, which is not allowed. "
                "Name each graph explicitly, for example GRAPH <urn:...> { ... }."
            )
        if "no readgrant" in d or "no invokegrant" in d:
            return (
                "You do not have permission to read that graph. "
                "Ask the dataset owner for access, or use a graph you have been given."
            )
        if "grantsreplace" in d:
            return (
                "You can add to that graph but not replace what is in it. "
                "Add your changes instead, or ask the dataset owner."
            )
        if "grantswrite" in d or "writes are never role-based" in d:
            return (
                "You do not have permission to write to that graph. "
                "You can write to your own scratch area; ask the dataset owner if you need more."
            )
        if "audit graph" in d:
            return "The audit log can only be read by an administrator, and only the bridge writes to it."
        if "admin role" in d:
            return "That action needs an administrator."
        return "You do not have permission to do that. Ask the dataset owner if you think you should."

    if status == 400:
        if "use /sparql/select" in d or "use /sparql/update" in d:
            return (
                "That is the wrong kind of query for this tool: a read query goes to the "
                "select or construct tool, and a change goes to the update tool."
            )
        if _validation_path(path):
            if "requires target_graph" in d:
                return "Name the graph to check (target_graph) when using delta mode."
            if "shacl" in d or "shapes" in d or "does not exist" in d:
                return (
                    "The checker could not use that shapes graph. Name a shapes graph that "
                    "exists and holds readable shapes, for example the dataset's own."
                )
        return "The request was not understood. Check the Turtle, the query and the graph name."

    if status == 404:
        return "That graph or item was not found. Check the name."

    if status == 405:
        return "That tool does not accept this kind of request. Try a different tool or ask for help."

    if status == 409:
        return "Someone else changed this at the same time. Read it again and retry."

    if status == 413:
        return "That is too large to send in one go. Split it into smaller parts."

    if status == 422:
        if _validation_path(path):
            return (
                "The checker could not read what you sent. Check that the Turtle parses "
                "and that every required field is filled in."
            )
        return "A required field is missing or has the wrong form. Check what you sent."

    if status == 429:
        return "Too many requests just now. Wait a moment and retry."

    if status in (502, 503, 504):
        return "The graph store did not answer. Wait a minute and retry; tell the owner if it keeps happening."

    if status >= 500:
        if _validation_path(path):
            return (
                "The checker failed on this data. If your Turtle uses RDF 1.2 triple terms "
                "(<<( ... )>>), that is a known limit of the checker: use a shapes graph "
                "that does not need them, or send the data without those annotations."
            )
        return "The bridge hit an internal error. Retry once, then tell the owner with the time."

    return None
