"""Bootstrap admin identity -- solves the AnimusDep chicken-and-egg problem.

Every identity-admin route (``create_person``, ``assign_persona``) and, in
fact, every ``AnimusDep``-gated route at all, requires an already-resolved
Person to exist for the caller's identity before it will do anything. On a
genuinely fresh dataset -- no Person minted yet, not even the operator's
own -- there is no route through the REST API that can create that first
Person: the gate that would authorise the write is the very thing not yet
satisfied. Confirmed live (2026-08-26) against a fresh ``example``
dataset: ``push_turtle`` itself came back 401 ("no Person
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

CHANGED 2026-09-18: ``--dataset`` accepts a comma-separated list, not just
one name -- bootstrapping a fresh identity onto several datasets at once
(four, in the case that prompted this) used to mean four separate
invocations, one dataset name typo away from silently bootstrapping the
wrong one four times over. Comma-separated rather than ``action="append"``
to match the one other multi-value CLI-adjacent convention already in
this codebase (``MCP_ALLOWED_GITHUB_LOGINS``). Each name in the list gets
its own ``Conn`` (same bank, same slug/name/identity/role, different
dataset) and its own independent ``ensure_admin_person`` call -- one
dataset failing (unreachable, wrong bank, whatever) is reported and
skipped rather than aborting the rest, since the datasets in a batch like
this are typically unrelated to each other and there is no reason a
problem with one should block bootstrapping the others. The process exit
code is non-zero if *any* dataset in the batch failed, so a scripted
caller (a startup hook, say) still sees the batch as failed overall even
though the log shows which ones actually succeeded.

Omitting ``--dataset`` entirely still means exactly what it always has --
the bank's own single default dataset -- so nothing about the
single-dataset, single-invocation case changes for anyone not using the
new list form.
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


async def find_person(client: FusekiClient, conn: Conn, *, external_id: str) -> str | None:
    """The Person IRI carrying this external id, or ``None``."""
    query = f"""PREFIX holon: <{HOLON}>
SELECT ?person WHERE {{
  GRAPH <{conn.holons_graph}> {{
    ?person holon:hasExternalIdentity ?identity .
    ?identity holon:identifier {_literal(external_id)} .
  }}
}} LIMIT 1"""
    result = await client.select(conn, query)
    bindings = result.get("results", {}).get("bindings", [])
    return bindings[0]["person"]["value"] if bindings else None


async def ensure_admin_person(
    client: FusekiClient,
    conn: Conn,
    *,
    slug: str,
    full_name: str,
    external_id: str,
    external_id_type: str = "GitHubIdentity",
    role: str | None = "owner",
    grant_existing: bool = False,
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
        if grant_existing and role:
            # Opt-in and additive only: adds the role, never removes or
            # changes anything. This is how a dataset that predates the owner
            # role (admins, no owner) gets its first owner -- the REST API
            # deliberately has no route that mints one.
            existing = await find_person(client, conn, external_id=external_id)
            if existing:
                role_iri = conn.role_iri(role)
                turtle = (
                    f"PREFIX holon: <{HOLON}>\nPREFIX rdfs: <{RDFS}>\n\n"
                    f"<{role_iri}> a holon:Role ;\n"
                    f'    rdfs:label {_literal(role.replace("-", " ").title())} .\n'
                    f"<{existing}> holon:hasRole <{role_iri}> .\n"
                )
                try:
                    await client.post_graph(conn, conn.holons_graph, turtle)
                except FusekiError as exc:
                    return {"ok": False, "detail": exc.as_dict()}
                return {
                    "ok": True,
                    "created": False,
                    "granted": True,
                    "person": existing,
                    "role": role_iri,
                }
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
        default="owner",
        help='Role slug to grant. Default "owner" (the first identity on a dataset '
        'is its owner; owner implies admin). Pass "admin" for a plain admin, or "" for no role.',
    )
    parser.add_argument(
        "--grant-existing",
        action="store_true",
        help="If this identity already has a Person, ADD --role to it instead of doing nothing. "
        "Additive only. Use it to give a dataset that predates the owner role its first owner.",
    )
    parser.add_argument(
        "--dataset",
        default=None,
        help="Dataset(s) to bootstrap into, comma-separated for more than one "
        '(e.g. "hcg,crm,datavid,ggsc"). Defaults to the bank\'s own dataset.',
    )
    parser.add_argument(
        "--bank",
        default="local",
        help='Named bank to connect through (see ~/.holonbridge/config.json). Default "local".',
    )
    return parser.parse_args(argv)


def _parse_datasets(raw: str | None) -> list[str | None]:
    """``--dataset``'s raw value -> the list of dataset names to bootstrap.

    ``None`` (the flag omitted) means exactly what it always has: one run
    against the bank's own default dataset, represented here as a
    single-item list holding ``None`` so the loop in ``_run`` doesn't need
    a separate code path for the single-dataset case. A comma-separated
    value splits and trims whitespace around each name; empty segments
    (a stray leading/trailing comma) are dropped rather than producing a
    confusing empty-string dataset name.
    """
    if raw is None:
        return [None]
    names = [name.strip() for name in raw.split(",")]
    return [name for name in names if name] or [None]


async def _bootstrap_one(
    client: FusekiClient, *, bank_name: str, dataset: str | None, settings: Settings, args: argparse.Namespace
) -> bool:
    """Bootstrap a single dataset; returns whether it succeeded. Never
    raises -- every failure mode (unknown bank, unreachable server, a
    FusekiError from the write itself) is caught and printed, so one bad
    dataset in a ``--dataset a,b,c`` batch doesn't take the rest down with
    it."""
    try:
        conn = _build_conn(bank_name=bank_name, dataset=dataset, settings=settings)
    except KeyError:
        print(f"error: no bank named {bank_name!r} configured", file=sys.stderr)
        return False

    try:
        result = await ensure_admin_person(
            client,
            conn,
            slug=args.slug,
            full_name=args.name,
            external_id=args.external_id,
            external_id_type=args.external_id_type,
            role=args.role or None,
            grant_existing=getattr(args, "grant_existing", False),
        )
    except FusekiError as exc:
        # ensure_admin_person already catches the write's own FusekiError
        # and returns it as {"ok": False, ...} -- this is for a connect-time
        # failure (the server unreachable, DNS, etc.) from person_exists's
        # own read, which happens before that try/except is ever entered.
        print(f"error: dataset {conn.dataset!r}: {exc}", file=sys.stderr)
        return False

    if not result.get("ok"):
        print(f"error: dataset {conn.dataset!r}: {result.get('detail')}", file=sys.stderr)
        return False
    if result.get("granted"):
        print(
            f"[bootstrap-admin] granted {result['role']} to existing {result['person']} "
            f"in dataset {conn.dataset!r}"
        )
    elif result.get("created"):
        print(
            f"[bootstrap-admin] created {result['person']} "
            f"(role: {result.get('role') or 'none'}) in dataset {conn.dataset!r}"
        )
    else:
        print(
            f"[bootstrap-admin] {args.external_id!r} already has an identity "
            f"in dataset {conn.dataset!r} -- no change"
        )
    return True


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    datasets = _parse_datasets(args.dataset)

    client = FusekiClient(timeout=settings.request_timeout)
    try:
        results = [
            await _bootstrap_one(
                client, bank_name=args.bank, dataset=dataset, settings=settings, args=args
            )
            for dataset in datasets
        ]
    finally:
        await client.aclose()

    if len(datasets) > 1:
        succeeded = sum(results)
        print(f"[bootstrap-admin] {succeeded}/{len(datasets)} dataset(s) succeeded")

    return 0 if all(results) else 1


def main() -> None:  # pragma: no cover
    args = _parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":  # pragma: no cover
    main()
