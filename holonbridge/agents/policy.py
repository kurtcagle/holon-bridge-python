"""Where an agent call may go, and how credentials are kept out of the graph.

Two separate problems, and the second is easy to get wrong.

**Where.** An endpoint is a URL somebody typed in; the bridge will POST graph
data to it. The webhook egress policy (``holonbridge/egress.py``) already
answers "is this a place the bridge may send to": https, public addresses, no
redirects, or an operator allowlist. Agents use the same mechanism with their
own allowlist (``AGENT_ALLOWLIST``) -- what a projection may be pushed to and
what a task may be handed to are separate trust decisions, so one list does not
stand in for the other.

**Credentials.** An endpoint refers to its secret as ``env:NAME``; the value is
read from the process environment at the moment of sending and appears nowhere
else: not in the graph, not in the call ledger, not in an error message, not in
a response. That alone is not enough. Registering an endpoint is a graph write,
so anyone who can register one could otherwise write ``env:DATABASE_PASSWORD``
against a host they control and have the bridge deliver it. Two rules close
that:

* the variable name must start with the operator's prefix
  (``AGENT_CREDENTIAL_PREFIX``, default ``HB_AGENT_``), so only variables the
  operator provisioned *for agents* are reachable at all; and
* an endpoint that carries credentials must match an ``AGENT_ALLOWLIST`` entry.
  With no allowlist configured, endpoints may exist (unauthenticated, to public
  https hosts) but may not carry a credential: a secret only travels to a host
  the operator named.

Both are checked when the endpoint is registered *and* again when a call is
sent, because the endpoint is data in the graph and the settings can change
after it was stored.
"""

from __future__ import annotations

import os
import re

from ..config import Settings, get_settings
from ..egress import EgressPolicy, EndpointRefused, check_endpoint, parse_endpoint

__all__ = [
    "CredentialRefused",
    "check_agent_endpoint",
    "parse_credential_ref",
    "resolve_secret",
]

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class CredentialRefused(ValueError):
    """A credential reference the bridge will not follow. Safe to show a caller:
    it names the reference, never a value."""


def parse_credential_ref(ref: str, *, settings: Settings | None = None) -> str:
    """``env:HB_AGENT_ACME`` -> ``HB_AGENT_ACME``, or raise."""
    settings = settings or get_settings()
    text = (ref or "").strip()
    scheme, sep, name = text.partition(":")
    if not sep or scheme != "env":
        raise CredentialRefused(
            f"credential reference {text!r} must look like env:NAME "
            "(only environment variables are supported)"
        )
    if not _ENV_NAME.match(name):
        raise CredentialRefused(
            f"credential reference {text!r}: {name!r} is not an upper-case "
            "environment variable name"
        )
    prefix = settings.agent_credential_prefix
    if not name.startswith(prefix):
        raise CredentialRefused(
            f"credential reference {text!r} is outside the allowed namespace: "
            f"agent credentials must be environment variables starting with {prefix!r}"
        )
    return name


def resolve_secret(ref: str, *, settings: Settings | None = None) -> str:
    """The secret a reference points at. The error never contains the value."""
    name = parse_credential_ref(ref, settings=settings)
    value = os.environ.get(name, "")
    if not value:
        raise CredentialRefused(f"credential {ref!r} is not set in the bridge's environment")
    return value


async def check_agent_endpoint(
    url: str, *, has_credentials: bool, settings: Settings | None = None, resolver=None
) -> None:
    """Raise :class:`EndpointRefused` unless a call may be sent to ``url``."""
    settings = settings or get_settings()
    allowlist = settings.agent_allowlist

    if has_credentials:
        if not allowlist:
            raise EndpointRefused(
                "this endpoint carries credentials, and no AGENT_ALLOWLIST is configured: "
                "a credential is only sent to a host the operator has listed"
            )
        # The allowlist check inside check_endpoint is what ties the credential
        # to a named host; parse first so the message is about the URL if the
        # URL is the problem.
        parse_endpoint(url)

    await check_endpoint(
        url,
        EgressPolicy(allowlist=allowlist, setting="AGENT_ALLOWLIST", label="agent allowlist"),
        resolver=resolver,
    )
