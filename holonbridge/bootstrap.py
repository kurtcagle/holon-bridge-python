"""Bootstrap admin identity -- solves the AnimusDep chicken-and-egg problem.

Every identity-admin route (``create_person``, ``assign_persona``) and, in
fact, every ``AnimusDep``-gated route at all, requires an already-resolved
Person to exist for the caller's identity before it will do anything. On a
genuinely fresh dataset -- no Person minted yet, not even the operator's
own -- there is no route through the REST API that can create that first
Person: the gate that would authorise the write is the very thing not yet
satisfied. Confirmed live (2026-08-26) against a fresh ``causalspark``
dataset on vm-fuseki: ``push_turtle`` itself came back 401 ("no Person
found for GitHubIdentity identifier ..."), same as every other route --
the bootstrap bypass that unblocked it that day was a raw SPARQL UPDATE
run directly against Fuseki, outside holon-bridge-python entirely.

This module is that same bypass, done properly and made repeatable. It
talks to Fuseki directly via ``FusekiClient``/``Conn``, exactly as every
route handler does internally, but never constructs or passes through the
FastAPI app or its ``AnimusDep`` dependency -- there is no HTTP request to
the bridge here at all, so there is nothing for ``AnimusDep`` to gate.

Deliberately narrow, on purpose:

* **Idempotent.** ``ensure_admin_person`` checks whether *any* Person in
  the target dataset already carries the given external id before writing
  anything, so it is safe to invoke unconditionally on every startup once
  configured -- a no-op once the identity exists, never a duplicate.
* **Never overwrites.** If the identity already exists, this does not
  touch, re-grant, or revoke any role -- someone may have been
  deliberately promoted or demoted since (see the 2026-08-26 decision to
  keep ``admin`` narrowly held rather than grant it broadly). This is
  specifically the *first-identity* bootstrap, not an ongoing sync.
  Every subsequent Person should go through the ordinary, now-unblocked
  admin route (``POST /admin/person``) once this one exists to authorise
  it -- this module is the one deliberate exception to that path, not a
  replacement for it.

Exposed as the ``holonbridge-bootstrap-admin`` console script (see
``pyproject.toml``), and callable from ``scripts/holonbridge-ctl.sh`` and
``scripts/start-holonbridge.ps1`` via new, opt-in ``BOOTSTRAP_ADMIN_*``
parameters -- unset by default, so nothing changes for anyone who doesn't
ask for this.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import BankStore, Settings, get_settings
from .conn import Conn
from .fuseki import FusekiClient, FusekiError
from .turtle import escape_literal

HOLON = "https://w3id.org/holon/"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"


def _literal(value: str) -> str:
    return f'"{escape_literal(value)}"'


async def person_exists(client: FusekiClient, conn: Conn, *, external_id: str) -> bool:
    """Whether any Person in this dataset already carries this external id.

    Checked by identity, not by slug -- a bootstrap re-run under a
    different slug for the same real person is still caught as a
    duplicate rather than minting a second Person for them.
    """
    query = f"""PREFIX holon: <{HOLON}>
ASK {{
  GRAPH <{conn.holons_graph}> {{
    ?person holon:hasExternalIdentity ?identity .
    ?identity holon:identifier {_literal(external_id)} .
  }}
}}"""
    result = await client.select(conn, query)
    return bool(result.get("boolean"))


async def ensure_admin_person(
    client: FusekiClient,
    conn: Conn,
    *,
    slug: str,
    full_name: str,
    external_id: str,
    external_id_type: str = "GitHubIdentity",
    role: str | None = "admin",
) -> dict[str, object]:
    """Mint a Person + external identity (+ optional Role grant) directly
    against Fuseki, bypassing AnimusDep entirely -- see module docstring.

    A no-op if a Person with this ``external_id`` already exists anywhere
    in the dataset's holons graph. Mirrors ``routes/identity_admin.py``'s
    ``create_person`` shape exactly (same Person/Identity/Role Turtle,
    same ``conn.person_iri``/``conn.role_iri`` helpers) so a Person minted
    here is indistinguishable from one minted the ordinary way once the
    ordinary way is unblocked.
    """
    if await person_exists(client, conn, external_id=external_id):
        return {"ok": True, "created": False, "reason": "external_id already present"}

    person = conn.person_iri(slug)
    id_type_slug = external_id_type.removesuffix("Identity").lower() or "identity"
    identity = f"{person}:identity:{id_type_slug}"

    role_iri = conn.role_iri(role) if role else None
    role_clause = f" ;\n    holon:hasRole <{role_iri}>" if role_iri else ""

    lines = [
        f"PREFIX holon: <{HOLON}>",
        f"PREFIX rdfs: <{RDFS}>",
        "",
        f"<{person}> a holon:Person ;",
        f"    rdfs:label {_literal(full_name)} ;",
        f"    holon:hasExternalIdentity <{identity}>{role_clause} .",
        "",
        f"<{identity}> a holon:{external_id_type} ;",
        f"    holon:identifier {_literal(external_id)} .",
    ]
    if role_iri:
        lines += [
            "",
            f"<{role_iri}> a holon:Role ;",
            f'    rdfs:label {_literal(role.replace("-", " ").title())} .',
        ]

    turtle = "\n".join(lines) + "\n"
    try:
        await client.post_graph(conn, conn.holons_graph, turtle)
    except FusekiError as exc:
        return {"ok": False, "detail": exc.as_dict()}

    return {
        "ok": True,
        "created": True,
        "person": person,
        "identity": identity,
        "role": role_iri,
    }


def _build_conn(*, bank_name: str, dataset: str | None, settings: Settings) -> Conn:
    banks = BankStore(settings)
    bank = banks.get(bank_name)
    return Conn(
        base_url=bank.url,
        dataset=dataset or bank.dataset,
        overridden=bool(dataset and dataset != bank.dataset),
        bank_name=bank.name,
        token=bank.auth_token,
        bank_scoped_datasets=bank.bank_scoped_datasets,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="holonbridge-bootstrap-admin",
        description=(
            "Mint one Person's identity directly against Fuseki, bypassing "
            "AnimusDep -- for the very first identity on a fresh dataset, "
            "where no route through the REST API can do it. Idempotent: a "
            "no-op if the external id already has a Person."
        ),
    )
    parser.add_argument("--slug", required=True, help='Local identifier, e.g. "kurt".')
    parser.add_argument("--name", required=True, help="Display name (rdfs:label).")
    parser.add_argument(
        "--github-user",
        required=True,
        dest="external_id",
        help="External identity (e.g. GitHub login).",
    )
    parser.add_argument(
        "--external-id-type",
        default="GitHubIdentity",
        help="holon: class of the external identity. Default GitHubIdentity.",
    )
    parser.add_argument(
        "--role",
        default="admin",
        help='Role slug to grant, e.g. "admin". Pass "" for no role.',
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help="Dataset to bootstrap into. Defaults to the bank's own dataset.",
    )
    parser.add_argument(
        "--bank",
        default="local",
        help='Named bank to connect through (see ~/.holonbridge/config.json). Default "local".',
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    try:
        conn = _build_conn(bank_name=args.bank, dataset=args.dataset, settings=settings)
    except KeyError:
        print(f"error: no bank named {args.bank!r} configured", file=sys.stderr)
        return 1

    client = FusekiClient(timeout=settings.request_timeout)
    try:
        result = await ensure_admin_person(
            client,
            conn,
            slug=args.slug,
            full_name=args.name,
            external_id=args.external_id,
            external_id_type=args.external_id_type,
            role=args.role or None,
        )
    finally:
        await client.aclose()

    if not result.get("ok"):
        print(f"error: {result.get('detail')}", file=sys.stderr)
        return 1
    if result.get("created"):
        print(
            f"[bootstrap-admin] created {result['person']} "
            f"(role: {result.get('role') or 'none'}) in dataset {conn.dataset!r}"
        )
    else:
        print(
            f"[bootstrap-admin] {args.external_id!r} already has an identity "
            f"in dataset {conn.dataset!r} -- no change"
        )
    return 0


def main() -> None:  # pragma: no cover
    args = _parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":  # pragma: no cover
    main()
