"""Authentication and request-scoped dependencies.

Everything a handler needs arrives through :func:`require_conn`: the caller
is authenticated, the dataset override is resolved, and the backend client
is attached. A route that forgets to depend on it gets no ``Conn`` at all,
which is the point -- the override gap closes by construction.

CHANGED 2026-08-15: added :func:`require_animus`. ``require_auth`` is a
single shared bearer token -- it answers "is this caller allowed to talk to
the bridge at all", the same for every caller. It was never a per-person
identity, and nothing before this added one. ``require_animus`` is that:
a caller now additionally presents *who* they are (an external identity,
e.g. a GitHub login), which gets resolved against this request's own
dataset to a Person, then to whatever Roles and grants that Person holds.
The two checks are independent and both run -- a valid bearer token gets
you in the door, a resolved animus gets you access to something once
you're through it. See the ACL architecture DataBook for the model this
resolves against.

CHANGED 2026-08-17: added :func:`get_personas`. Session state for "which
persona is this person currently reading as" -- see persona_state.py for
why it's keyed by Animus.person rather than living in the MCP layer
alongside the dataset/bank overrides. One ``PersonaStore`` per process,
same lifetime as ``app.state.fuseki``.

CHANGED 2026-09-01: added :func:`get_acting_as`, and ``require_animus``
now consults it. An admin who has called ``/admin/act-as`` gets every
subsequent request on their credential resolved as their chosen target
instead of themselves -- see ``acting_as.py`` for why that alone is
enough to route the request through the real (non-bypassed) grant-check
code, and for why the real identity is always resolved first and checked
for a still-current admin role before any override is honoured. The
returned ``Animus`` always carries ``real_person``/``real_person_label``
alongside ``person`` -- equal to it, with ``acting_as`` False, when no
override is active -- so any caller (route or MCP tool) that specifically
needs the authenticated identity rather than the currently-acting-as one
has a field to read instead of having to know about this store directly.

CHANGED 2026-10-07: :func:`require_auth` now classifies the token
(:class:`TokenAuth`) and :func:`require_animus` honours an identity-bound token:
the credential, not the ``x-holon-animus-id`` header, decides who the caller is.
The shared ``BEARER_TOKEN`` keeps its old meaning (any identity via the header)
unless ``ALLOW_LEGACY_BEARER`` is turned off.

CHANGED 2026-09-30 (audit): ``require_conn`` and ``require_animus`` also
put the resolved ``Conn`` and ``Animus`` on ``request.state``, which is
where ``holonbridge.audit.AuditMiddleware`` reads them after the response.
The middleware runs outside the dependency graph, so this is the only way
it can learn who acted, and on which dataset.
"""

from __future__ import annotations

import dataclasses
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from .acl import Animus, build_animus, build_animus_as, is_admin
from .acting_as import ActingAsStore
from .config import BankStore, Settings
from .conn import DATASET_OVERRIDE_HEADER, Conn, resolve_conn
from .cache import RegistryCache
from .claim import claimant as claim_eligibility, is_unclaimed
from .fuseki import FusekiClient
from .persona_state import PersonaStore

ANIMUS_ID_HEADER = "x-holon-animus-id"
ANIMUS_TYPE_HEADER = "x-holon-animus-type"
DEFAULT_ANIMUS_TYPE = "GitHubIdentity"


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_banks(request: Request) -> BankStore:
    return request.app.state.banks


def get_client(request: Request) -> FusekiClient:
    return request.app.state.fuseki


def get_registry(request: Request) -> RegistryCache:
    return request.app.state.registry


def get_personas(request: Request) -> PersonaStore:
    return request.app.state.personas


def get_acting_as(request: Request) -> ActingAsStore:
    return request.app.state.acting_as


#: The only kinds :func:`require_auth` can produce. Anything else is a bug.
TOKEN_KINDS = frozenset({"identity", "proxy", "legacy", "open"})


@dataclasses.dataclass(frozen=True)
class TokenAuth:
    """What the presented bearer token IS, as decided by :func:`require_auth`.

    ``kind``:

    - ``identity`` -- bound to ``identity``; the caller IS that identity and no
      header can say otherwise.
    - ``proxy`` -- held by a trusted proxy (the MCP remote, which has verified
      the caller itself); may assert an identity via ``x-holon-animus-id``.
    - ``legacy`` -- the shared ``BEARER_TOKEN``; may assert ANY identity via the
      header, admin included. This is the pre-existing behaviour, kept so a
      deployment can migrate; see ``allow_legacy_bearer`` to switch it off.
    - ``open`` -- no token configured (loopback development only).
    """

    kind: str
    identity: str | None = None


def _match_token(presented: str, settings: Settings) -> TokenAuth | None:
    """Compare against EVERY configured token with no early exit, so response
    time does not reveal which kind (or how many) matched."""
    given = presented.encode("utf-8")
    found: TokenAuth | None = None
    for token, identity in settings.identity_tokens.items():
        if secrets.compare_digest(given, token.encode("utf-8")):
            found = TokenAuth("identity", identity)
    for token in settings.proxy_tokens:
        if secrets.compare_digest(given, token.encode("utf-8")):
            found = TokenAuth("proxy")
    if settings.bearer_token and settings.allow_legacy_bearer:
        if secrets.compare_digest(given, settings.bearer_token.encode("utf-8")):
            found = TokenAuth("legacy")
    return found


def require_auth(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    authorization: Annotated[str | None, Header()] = None,
) -> TokenAuth:
    """Bearer check. Runs before anything touches the backend.

    Also classifies the token (:class:`TokenAuth`) and leaves the result on
    ``request.state.token_auth`` for :func:`require_animus`, which is where an
    identity-bound token actually takes effect.
    """
    configured = bool(
        settings.bearer_token or settings.identity_tokens or settings.proxy_tokens
    )
    if not configured:
        # No token configured: local development only. Refuse to serve
        # anything if the process is listening on a non-loopback interface.
        if settings.host not in {"127.0.0.1", "localhost", "::1"}:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="BEARER_TOKEN must be set when binding a public interface",
            )
        result = TokenAuth("open")
        request.state.token_auth = result
        return result

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    presented = authorization.split(" ", 1)[1].strip()
    result = _match_token(presented, settings)
    if result is None:
        # Same message whether the token is unknown or is the legacy token on a
        # deployment that has switched it off: do not say which.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.token_auth = result
    return result


def require_conn(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    banks: Annotated[BankStore, Depends(get_banks)],
    _: Annotated[TokenAuth, Depends(require_auth)],
) -> Conn:
    """Resolve the connection for this request."""
    override = request.headers.get(DATASET_OVERRIDE_HEADER)
    bank_name = request.query_params.get("bank")
    try:
        conn = resolve_conn(
            settings=settings,
            banks=banks,
            override=override,
            bank_name=bank_name,
        )
    except KeyError as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"unknown bank: {exc.args[0]}"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    request.state.conn = conn  # read by holonbridge.audit.AuditMiddleware
    return conn


async def require_animus(
    request: Request,
    conn: Annotated[Conn, Depends(require_conn)],
    client: Annotated[FusekiClient, Depends(get_client)],
    acting_as: Annotated[ActingAsStore, Depends(get_acting_as)],
    token_auth: Annotated[TokenAuth, Depends(require_auth)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> Animus:
    """Resolve the calling Person (and their Teams) for this request.

    A missing or unresolvable identity is a 401, not an anonymous pass-through
    -- there is no route that should run ACL-gated logic against an
    ``Animus`` with ``person=None`` and treat that as "allow everything";
    callers that need that decision explicitly available should depend on
    :func:`require_conn` alone and skip this dependency, not lean on this
    one failing to resolve.

    Always resolves the REAL presented identity first, in full, before
    consulting :data:`acting_as` -- an active act_as override is only ever
    layered on top of that real resolution, never a substitute for it.
    Two things fall out of doing it in that order: (1) the real identity's
    admin role is re-checked on every request the override applies to, so
    a role revoked after ``act_as`` was called drops the override on its
    very next use rather than leaving it live until someone notices; (2)
    any caller that needs the authenticated identity regardless of an
    active override -- see ``real_person`` below, and the
    ``/admin/act-as``/``/admin/cease-acting-as`` routes themselves -- has
    it, unconditionally, on every returned ``Animus``.
    """
    # ``token_auth`` is injected, not read back from request state with a default:
    # a missing classification used to fall back to the most permissive kind
    # ("legacy"), which fails OPEN. There is no such path now -- the dependency
    # either classifies the token or the request is refused -- and an
    # unrecognised kind is an error, never a quiet downgrade.
    header_id = request.headers.get(ANIMUS_ID_HEADER)
    if token_auth.kind not in TOKEN_KINDS:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"unrecognised token classification {token_auth.kind!r}",
        )
    if token_auth.kind == "identity":
        # The credential decides who this is; the header may only agree with it.
        # Never honour a different one, and ignore the type header too -- else a
        # holder could present the same string under another identity namespace.
        if header_id and header_id != token_auth.identity:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=f"this token is bound to a different identity than the {ANIMUS_ID_HEADER} header names",
            )
        external_id = token_auth.identity
        external_id_type = DEFAULT_ANIMUS_TYPE
    else:
        external_id = header_id
        if not external_id:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail=f"missing {ANIMUS_ID_HEADER} header",
            )
        external_id_type = request.headers.get(ANIMUS_TYPE_HEADER, DEFAULT_ANIMUS_TYPE)

    async def query_fn(query: str) -> dict:
        return await client.select(conn, query)

    real = await build_animus(
        query_fn,
        conn.holons_graph,
        external_id=external_id,
        external_id_type=external_id_type,
    )
    if real.person is None:
        # An unprovisioned dataset looks identical to "you are not allowed" unless
        # we say otherwise -- but only to a caller who could act on it. Anyone else
        # gets the plain 401, so this cannot be used to ask which datasets are open.
        if claim_eligibility(
            settings,
            token_auth,
            header_id=header_id,
            header_type=request.headers.get(ANIMUS_TYPE_HEADER),
            default_type=DEFAULT_ANIMUS_TYPE,
        ) is not None and await is_unclaimed(query_fn, conn.holons_graph):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "dataset_unclaimed",
                    "message": (
                        f"dataset {conn.dataset!r} has no owner or admin, and "
                        f"{external_id!r} is permitted to initialise it"
                    ),
                    "remedy": "POST /admin/dataset/initialise (MCP tool: initialise_dataset)",
                    "ask_the_person": (
                        "Initialising makes the caller the dataset's owner. Confirm with the "
                        "person you are acting for, in chat, before calling it."
                    ),
                },
            )
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail=f"no Person found for {external_id_type} identifier {external_id!r}",
        )

    target = acting_as.get(real_person=real.person)
    if target is not None and not await is_admin(
        query_fn, conn.holons_graph, person=real.person
    ):
        # Admin role was revoked (or never held on this dataset) since
        # act_as was last called -- fail safe and drop the stale override
        # rather than silently keep honouring it.
        acting_as.clear(real_person=real.person)
        target = None

    if target is None:
        animus = dataclasses.replace(
            real, real_person=real.person, real_person_label=real.person_label
        )
        request.state.animus = animus  # read by holonbridge.audit.AuditMiddleware
        return animus

    impersonated = await build_animus_as(query_fn, conn.holons_graph, person=target)
    animus = dataclasses.replace(
        impersonated,
        real_person=real.person,
        real_person_label=real.person_label,
        acting_as=True,
    )
    request.state.animus = animus  # read by holonbridge.audit.AuditMiddleware
    return animus


ConnDep = Annotated[Conn, Depends(require_conn)]
ClientDep = Annotated[FusekiClient, Depends(get_client)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
BanksDep = Annotated[BankStore, Depends(get_banks)]
RegistryDep = Annotated[RegistryCache, Depends(get_registry)]
AnimusDep = Annotated[Animus, Depends(require_animus)]
PersonasDep = Annotated[PersonaStore, Depends(get_personas)]
ActingAsDep = Annotated[ActingAsStore, Depends(get_acting_as)]
