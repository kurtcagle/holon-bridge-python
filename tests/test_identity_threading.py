"""Tests for the identity threading added 2026-08-15: BearerGate capturing
which credential was presented, not just whether it was valid, and that
identity surviving into a task spawned the way mcp.server.lowlevel.server
actually spawns one (tg.start_soon, not a bare await).

The second half is the part worth actually testing rather than trusting
from reading asyncio's docs: this whole design rests on the claim that
task-group-spawned tasks inherit the context active when they were
spawned. Confirmed against real asyncio here, not just asserted.
"""

from __future__ import annotations

import asyncio
import contextvars
import unittest

from holonbridge_mcp.identity import current_github_login
from holonbridge_mcp.remote import (
    REJECTED,
    _github_oauth_checker,
    _static_token_checker,
    BearerGate,
)


class _FakeOAuthConfig:
    """Stand-in so the checker can be tested without real GitHub calls."""


class CheckerTests(unittest.TestCase):
    def test_static_token_checker_accepts_matching_token(self) -> None:
        check = _static_token_checker("correct-horse-battery-staple")
        self.assertIsNone(check("correct-horse-battery-staple"))

    def test_static_token_checker_rejects_wrong_token(self) -> None:
        check = _static_token_checker("correct-horse-battery-staple")
        self.assertIs(check("wrong"), REJECTED)

    def test_oauth_checker_returns_login_on_valid_token(self) -> None:
        import holonbridge_mcp.github_oauth as github_oauth

        original = github_oauth.verify_session_token
        github_oauth.verify_session_token = lambda cfg, presented: (
            "kurtcagle" if presented == "good-jwt" else None
        )
        try:
            check = _github_oauth_checker(_FakeOAuthConfig())
            self.assertEqual(check("good-jwt"), "kurtcagle")
            self.assertIs(check("bad-jwt"), REJECTED)
        finally:
            github_oauth.verify_session_token = original


class BearerGateAuthorisationTests(unittest.TestCase):
    def _scope(self, auth_header: bytes | None) -> dict:
        headers = [(b"authorization", auth_header)] if auth_header else []
        return {"type": "http", "path": "/sse", "headers": headers}

    def test_no_header_is_rejected(self) -> None:
        gate = BearerGate(app=lambda *a: None, checkers=[_static_token_checker("x" * 24)], open_paths=())
        self.assertIs(gate._authorised(self._scope(None)), REJECTED)

    def test_static_token_authorises_with_no_identity(self) -> None:
        gate = BearerGate(
            app=lambda *a: None, checkers=[_static_token_checker("x" * 24)], open_paths=()
        )
        result = gate._authorised(self._scope(b"Bearer " + b"x" * 24))
        self.assertIsNone(result)  # valid, no identity -- not REJECTED

    def test_oauth_checker_wins_over_static_when_static_rejects(self) -> None:
        import holonbridge_mcp.github_oauth as github_oauth

        original = github_oauth.verify_session_token
        github_oauth.verify_session_token = lambda cfg, presented: "caroline-login"
        try:
            gate = BearerGate(
                app=lambda *a: None,
                checkers=[_static_token_checker("x" * 24), _github_oauth_checker(_FakeOAuthConfig())],
                open_paths=(),
            )
            result = gate._authorised(self._scope(b"Bearer some-jwt-not-the-static-token"))
            self.assertEqual(result, "caroline-login")
        finally:
            github_oauth.verify_session_token = original


class ContextPropagationTests(unittest.IsolatedAsyncioTestCase):
    """The load-bearing claim: a contextvar set before a task-group-style
    spawn is visible inside that spawned task, the same shape as
    mcp.server.lowlevel.server.Server.run's tg.start_soon(self._handle_message, ...).
    """

    async def test_contextvar_set_before_create_task_is_visible_inside_it(self) -> None:
        current_github_login.set("kurtcagle")

        seen: list[str | None] = []

        async def spawned_handler() -> None:
            # This is the analogue of _handle_message running inside the
            # task Server.run() spawns per incoming message.
            seen.append(current_github_login.get())

        # asyncio.create_task is what anyio's start_soon uses under the
        # asyncio backend; both copy the *current* context at spawn time.
        task = asyncio.create_task(spawned_handler())
        await task

        self.assertEqual(seen, ["kurtcagle"])

    async def test_contextvar_set_after_spawn_does_not_reach_an_already_running_task(self) -> None:
        # This is the case that makes per-message (POST /messages/) identity
        # NOT what propagates -- only the identity active when the
        # long-lived session task group itself was created does.
        started = asyncio.Event()
        seen: list[str | None] = []

        async def spawned_handler() -> None:
            started.set()
            await asyncio.sleep(0.05)
            seen.append(current_github_login.get())

        current_github_login.set("kurtcagle")
        task = asyncio.create_task(spawned_handler())
        await started.wait()

        # Simulate a *different* request's BearerGate setting a *different*
        # identity in what is, in the real system, a separate ASGI call
        # entirely -- this must NOT be what the already-spawned task sees.
        current_github_login.set("someone-else")
        await task

        self.assertEqual(seen, ["kurtcagle"])

    async def test_reset_does_not_leak_into_a_sibling_context(self) -> None:
        # BearerGate.__call__ does current_github_login.set(...) then
        # .reset(token) in a finally. Confirms that pattern actually
        # isolates one "request" from the next when they run as separate
        # top-level calls (not nested tasks) -- the ordinary case for two
        # unrelated incoming requests handled one after another.
        async def one_request(identity: str) -> str | None:
            token = current_github_login.set(identity)
            try:
                return current_github_login.get()
            finally:
                current_github_login.reset(token)

        first = await one_request("kurtcagle")
        after_reset = current_github_login.get()
        second = await one_request("ctownley-cs")

        self.assertEqual(first, "kurtcagle")
        self.assertIsNone(after_reset)
        self.assertEqual(second, "ctownley-cs")


if __name__ == "__main__":
    unittest.main()
