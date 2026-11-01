"""Sending one agent call over HTTP.

The only place a credential is ever read. Everything around it is arranged so
that a secret cannot leak by accident:

* it is resolved from the environment inside :meth:`HttpAgentSender.send`, held
  in a local, and put in a request header -- never in a return value, a log
  line, or an exception message (errors name the *reference*, not the value);
* the endpoint is checked again here, so a stored entry that predates a tighter
  allowlist, or that was written by a route that skipped registration, is
  refused rather than trusted;
* redirects are not followed, so a permitted host cannot bounce the bridge (and
  the credential with it) to one that is not;
* the response is read in a bounded way and abandoned if it exceeds the
  endpoint's cap, rather than buffered whole and then measured; and
* the whole exchange has one deadline, not one per network phase.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from ..config import Settings, get_settings
from ..egress import EndpointRefused
from .model import Call, Endpoint
from .policy import CredentialRefused, check_agent_endpoint, resolve_secret

__all__ = ["AgentSender", "HttpAgentSender", "Reply", "SendFailed", "sign"]


class SendFailed(RuntimeError):
    """The call did not complete. The message is safe to store and show."""


@dataclass(frozen=True)
class Reply:
    status_code: int
    content_type: str
    text: str
    size: int
    truncated: bool


class AgentSender(Protocol):
    async def send(self, endpoint: Endpoint, call: Call, body: str) -> Reply: ...


def sign(secret: str, timestamp: str, body: str) -> str:
    """``sha256=<hex>`` over ``"{timestamp}.{body}"``. The timestamp is part of
    what is signed so a captured request cannot be replayed with a fresh one."""
    mac = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


class HttpAgentSender:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver=None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._resolver = resolver

    async def send(self, endpoint: Endpoint, call: Call, body: str) -> Reply:
        settings = self._settings or get_settings()
        try:
            await check_agent_endpoint(
                endpoint.url,
                has_credentials=endpoint.has_credentials,
                settings=settings,
                resolver=self._resolver,
            )
            headers = self._headers(endpoint, call, body, settings)
        except EndpointRefused as exc:
            raise SendFailed(f"endpoint refused: {exc}") from exc
        except CredentialRefused as exc:
            raise SendFailed(str(exc)) from exc

        try:
            return await asyncio.wait_for(
                self._exchange(endpoint, headers, body),
                timeout=endpoint.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise SendFailed(f"no complete response within {endpoint.timeout_seconds:g}s") from exc
        except httpx.HTTPError as exc:
            # The exception type is informative and the text of an httpx error
            # does not carry request headers, but keep it short regardless.
            raise SendFailed(f"{type(exc).__name__}: {str(exc)[:200]}") from exc

    # ------------------------------------------------------------------------------

    def _headers(
        self, endpoint: Endpoint, call: Call, body: str, settings: Settings
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": call.id,
            "X-HolonBridge-Call": call.id,
        }
        if endpoint.auth_scheme == "bearer":
            headers["Authorization"] = f"Bearer {resolve_secret(endpoint.credential_ref, settings=settings)}"
        elif endpoint.auth_scheme == "header":
            headers[endpoint.auth_header] = resolve_secret(endpoint.credential_ref, settings=settings)
        if endpoint.signing_ref:
            timestamp = str(int(time.time()))
            headers["X-HolonBridge-Timestamp"] = timestamp
            headers["X-HolonBridge-Signature"] = sign(
                resolve_secret(endpoint.signing_ref, settings=settings), timestamp, body
            )
        return headers

    async def _exchange(self, endpoint: Endpoint, headers: dict[str, str], body: str) -> Reply:
        cap = endpoint.max_response_bytes
        async with httpx.AsyncClient(
            timeout=endpoint.timeout_seconds,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            async with client.stream(
                "POST", endpoint.url, content=body.encode("utf-8"), headers=headers
            ) as response:
                chunks: list[bytes] = []
                size = 0
                truncated = False
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > cap:
                        truncated = True
                        break
                    chunks.append(chunk)
                return Reply(
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type", ""),
                    text=b"".join(chunks).decode("utf-8", errors="replace"),
                    size=size,
                    truncated=truncated,
                )
